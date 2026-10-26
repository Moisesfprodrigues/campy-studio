import shlex
import struct
import time
import zlib
from datetime import datetime
from pathlib import Path

import pytest

import campy_studio as ca
from campy_studio import ErroTelemovel, Registo, Telemovel

CAMARA_FOCO = "mCurrentFocus=Window{1a2b u0 com.sec.android.app.camera/com.sec.android.app.camera.Camera}"
OUTRO_FOCO = "mCurrentFocus=Window{9f u0 NotificationShade}"


def png(largura: int, altura: int) -> bytes:
    def bloco(tipo: bytes, dados: bytes) -> bytes:
        crc = zlib.crc32(tipo + dados) & 0xFFFFFFFF
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", crc)

    cabecalho = struct.pack(">IIBBBBB", largura, altura, 8, 2, 0, 0, 0)
    linhas = (b"\x00" + b"\x80\x80\x80" * largura) * altura
    return (b"\x89PNG\r\n\x1a\n" + bloco(b"IHDR", cabecalho)
            + bloco(b"IDAT", zlib.compress(linhas)) + bloco(b"IEND", b""))


class FakeAdb:
    """Telemóvel Android simulado: responde aos comandos que a app usa."""

    def __init__(self):
        self.chamadas: list[tuple] = []
        self.ficheiros: dict[str, tuple[bytes, int]] = {}
        self.relogio = 1_798_000_000
        self.passo = 1
        self.camara_aberta = True
        self.dispara = True
        self.ecra = True
        self.truncar_pull = False
        self.dispositivos = "List of devices attached\nABC123\tdevice\n\n"
        self.definicoes = {
            ("global", "stay_on_while_plugged_in"): "0",
            ("system", "screen_off_timeout"): "60000",
        }
        self._seq = 0

    def adicionar(self, nome: str, dados: bytes = b"JPEGDATA", mtime: int | None = None) -> None:
        self.ficheiros[nome] = (dados, mtime if mtime is not None else self.relogio)

    def comandos(self) -> list[str]:
        return [" ".join(c) for c in self.chamadas]

    def __call__(self, *args, binario=False, timeout=60):
        self.chamadas.append(args)
        if args[0] == "devices":
            return self.dispositivos
        if args[0] == "pull":
            dados = self.ficheiros[Path(args[1]).name][0]
            Path(args[2]).write_bytes(dados[:-1] if self.truncar_pull else dados)
            return ""
        if args[0] == "exec-out":
            return png(1080, 2220)
        assert args[0] == "shell", args
        return self._shell(args[1:])

    def _shell(self, a: tuple) -> str:
        if a[0] == "ls":
            return "\n".join(self.ficheiros) + "\n"
        if a[0] == "stat":
            nome = Path(shlex.split(a[3])[0]).name
            dados, mtime = self.ficheiros[nome]
            return str(len(dados) if a[2] == "%s" else mtime)
        if a[0] == "rm":
            self.ficheiros.pop(Path(shlex.split(a[1])[0]).name)
            return ""
        if a[:2] == ("input", "keyevent"):
            if a[2] == "KEYCODE_WAKEUP":
                self.ecra = True
            elif a[2] == "KEYCODE_CAMERA" and self.dispara and self.camara_aberta:
                self._seq += 1
                self.adicionar(f"IMG_{self._seq}.jpg", f"foto{self._seq}".encode(), self.relogio)
                self.relogio += self.passo
            return ""
        if a[:2] == ("input", "tap"):
            return ""
        if a[0] == "dumpsys window | grep mCurrentFocus":
            return CAMARA_FOCO if self.camara_aberta else OUTRO_FOCO
        if a[0] == "dumpsys power | grep mWakefulness=":
            return "mWakefulness=Awake" if self.ecra else "mWakefulness=Asleep"
        if a[:2] == ("settings", "get"):
            return self.definicoes.get((a[2], a[3]), "null") + "\n"
        if a[:2] == ("settings", "put"):
            self.definicoes[(a[2], a[3])] = a[4]
            return ""
        if a[:3] == ("svc", "power", "stayon"):
            self.definicoes[("global", "stay_on_while_plugged_in")] = "7" if a[3] == "true" else "0"
            return ""
        if a[:2] == ("wm", "dismiss-keyguard"):
            return ""
        if a[:2] == ("am", "start"):
            self.camara_aberta = True
            self.ecra = True
            return "Starting: Intent\n"
        raise AssertionError(f"comando não simulado: {a}")


@pytest.fixture
def fake():
    return FakeAdb()


@pytest.fixture
def tel(fake):
    return Telemovel(adb=fake, dormir=lambda _s: None, agora=lambda: datetime(2026, 1, 2, 3, 4, 5))


@pytest.fixture
def raw(tmp_path):
    return tmp_path / "raw"


@pytest.fixture
def registo(tmp_path):
    return Registo(tmp_path / "baixados.txt")


def nome_esperado(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y%m%d_%H%M%S") + ".jpg"


# ------------------------------------------------------------ nomes e ficheiros

def test_reservar_nunca_substitui(tmp_path):
    a = ca.reservar(tmp_path, "20261026_201500")
    b = ca.reservar(tmp_path, "20261026_201500")
    c = ca.reservar(tmp_path, "20261026_201500")
    assert [p.name for p in (a, b, c)] == [
        "20261026_201500.jpg", "20261026_201500_1.jpg", "20261026_201500_2.jpg"]


def test_registo_persistente(registo):
    assert registo.nomes() == set()
    registo.adicionar("a.jpg")
    registo.adicionar("b.jpg")
    assert Registo(registo.ficheiro).nomes() == {"a.jpg", "b.jpg"}


@pytest.mark.parametrize("energia, esperado", [
    ({"stay": "7", "timeout": "1800000"}, "ecrã sempre ligado a carregar (tempo limite 30 min)"),
    ({"stay": "0", "timeout": "60000"}, "normal (o ecrã desliga em 1 min)"),
    ({"stay": None, "timeout": None}, "normal (o ecrã desliga em ?)"),
])
def test_descrever_energia(energia, esperado):
    assert ca.descrever_energia(energia) == esperado


# ------------------------------------------------------------------ disparo

def test_tirar_foto_descarrega_e_renomeia_por_data_hora(tel, fake, raw, registo):
    inicio = fake.relogio
    destino = tel.tirar_foto(raw, registo)
    assert destino == raw / nome_esperado(inicio)
    assert destino.read_bytes() == b"foto1"
    assert registo.nomes() == {"IMG_1.jpg"}
    assert "IMG_1.jpg" in fake.ficheiros  # por omissão não apaga do telemóvel


def test_duas_fotos_no_mesmo_segundo_nao_se_substituem(tel, fake, raw, registo):
    fake.passo = 0
    a = tel.tirar_foto(raw, registo)
    b = tel.tirar_foto(raw, registo)
    assert a != b
    assert {a.read_bytes(), b.read_bytes()} == {b"foto1", b"foto2"}
    assert len(list(raw.glob("*.jpg"))) == 2


def test_foco_por_toque_antes_do_disparo(tel, fake, raw, registo):
    tel.tirar_foto(raw, registo, foco=(540, 700))
    cmds = fake.comandos()
    tap = cmds.index("shell input tap 540 700")
    tecla = cmds.index("shell input keyevent KEYCODE_CAMERA")
    assert tap < tecla


def test_sem_foco_nao_toca_no_ecra(tel, fake, raw, registo):
    tel.tirar_foto(raw, registo, foco=None)
    assert not any(c[:3] == ("shell", "input", "tap") for c in fake.chamadas)


def test_abre_a_camara_se_estiver_fechada(tel, fake, raw, registo):
    fake.camara_aberta = False
    tel.tirar_foto(raw, registo)
    cmds = fake.comandos()
    assert any(c.startswith("shell am start -a android.media.action.STILL_IMAGE_CAMERA") for c in cmds)
    assert len(list(raw.glob("*.jpg"))) == 1


def test_erro_claro_se_a_camara_nao_gravar(tel, fake, raw, registo):
    fake.dispara = False
    with pytest.raises(ErroTelemovel, match="não gravou nenhuma foto"):
        tel.tirar_foto(raw, registo, timeout=2)
    assert not list(raw.glob("*"))


def test_apagar_so_depois_de_transferir(tel, fake, raw, registo):
    tel.tirar_foto(raw, registo, apagar=True)
    assert fake.ficheiros == {}
    assert len(list(raw.glob("*.jpg"))) == 1


def test_transferencia_incompleta_nao_apaga_nem_deixa_lixo(tel, fake, raw, registo):
    fake.truncar_pull = True
    with pytest.raises(ErroTelemovel, match="incompleta"):
        tel.tirar_foto(raw, registo, apagar=True)
    assert "IMG_1.jpg" in fake.ficheiros
    assert not list(raw.glob("*"))
    assert registo.nomes() == set()


def test_nunca_usa_apagar_recursivo(tel, fake, raw, registo):
    tel.tirar_foto(raw, registo, apagar=True)
    rm = [c for c in fake.chamadas if c[:2] == ("shell", "rm")]
    assert len(rm) == 1 and "-r" not in " ".join(rm[0]) and "-f" not in " ".join(rm[0])


# -------------------------------------------------------- descarregar todas

def test_descarregar_novas_ignora_registadas_e_nao_repete(tel, fake, raw, registo):
    fake.adicionar("20241126_195155(0).jpg", b"antiga", 1_700_000_000)
    fake.adicionar("20261026_202120.jpg", b"nova", 1_798_000_100)
    registo.adicionar("20261026_202120.jpg")
    nomes = tel.novas(registo)
    assert nomes == ["20241126_195155(0).jpg"]
    ok, ignoradas, erros = tel.descarregar_lista(nomes, raw, registo)
    assert (ok, ignoradas, erros) == (1, 0, [])
    assert (raw / nome_esperado(1_700_000_000)).read_bytes() == b"antiga"
    assert tel.novas(registo) == []


def test_nomes_com_parenteses_sao_citados_no_shell(tel, fake, raw, registo):
    fake.adicionar("foto(0).jpg")
    tel.descarregar("foto(0).jpg", raw, registo, apagar=True)
    stat = next(c for c in fake.chamadas if c[:2] == ("shell", "stat"))
    assert stat[-1].startswith("'") and stat[-1].endswith("'")


def test_listar_ignora_ficheiros_temporarios_e_nao_jpg(tel, fake):
    fake.adicionar("a.jpg")
    fake.adicionar(".pending-123-a.jpg")
    fake.adicionar("video.mp4")
    fake.adicionar("B.JPEG")
    assert sorted(tel.listar()) == ["B.JPEG", "a.jpg"]


# ---------------------------------------------------------------- energia

def test_manter_ligado_guarda_original_e_repoe(tel, fake):
    original = tel.manter_ligado(None)
    assert original == {"stay": "0", "timeout": "60000"}
    assert fake.definicoes[("global", "stay_on_while_plugged_in")] == "7"
    assert fake.definicoes[("system", "screen_off_timeout")] == str(ca.TIMEOUT_ECRA_MS)
    assert tel.repor_energia(original) is True
    assert fake.definicoes[("global", "stay_on_while_plugged_in")] == "0"
    assert fake.definicoes[("system", "screen_off_timeout")] == "60000"


def test_manter_ligado_repetido_nao_perde_o_original(tel, fake):
    original = tel.manter_ligado(None)
    de_novo = tel.manter_ligado(original)  # já com os valores alterados no telemóvel
    assert de_novo == {"stay": "0", "timeout": "60000"}


def test_repor_sem_original_so_desliga_o_ecra_sempre_ligado(tel, fake):
    fake.definicoes[("global", "stay_on_while_plugged_in")] = "7"
    fake.definicoes[("system", "screen_off_timeout")] = "1800000"
    assert tel.repor_energia(None) is False
    assert fake.definicoes[("global", "stay_on_while_plugged_in")] == "0"
    assert fake.definicoes[("system", "screen_off_timeout")] == "1800000"


def test_repor_respeita_um_original_nao_nulo(tel, fake):
    tel.repor_energia({"stay": "2", "timeout": "120000"})
    assert fake.definicoes[("global", "stay_on_while_plugged_in")] == "2"
    assert fake.definicoes[("system", "screen_off_timeout")] == "120000"


def test_abrir_camara_acorda_o_ecra(tel, fake):
    fake.camara_aberta = False
    fake.ecra = False
    assert tel.abrir_camara() is True
    assert fake.ecra is True


# ------------------------------------------------------------------ estado

def test_estado_completo(tel, fake):
    est = tel.estado()
    assert est["serial"] == "ABC123" and est["ligacao"] == "device"
    assert est["ecra"] and est["camara"]
    assert est["energia"] == {"stay": "0", "timeout": "60000"}


def test_estado_sem_autorizacao(tel, fake):
    fake.dispositivos = "List of devices attached\nABC123\tunauthorized\n\n"
    est = tel.estado()
    assert est["ligacao"] == "unauthorized" and not est["camara"]
    assert not any(c[0] == "shell" for c in fake.chamadas)


def test_estado_sem_telemovel(tel, fake):
    fake.dispositivos = "List of devices attached\n\n"
    assert tel.estado()["serial"] is None


def test_camara_fechada_detetada(tel, fake):
    fake.camara_aberta = False
    assert tel.camara_em_primeiro_plano() is False


def test_captura_de_ecra_devolve_png(tel):
    dados = tel.captura_ecra()
    assert dados[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(dados[16:20], "big") == 1080


# -------------------------------------------------------------- adb real

def test_adb_real_sem_adb_da_mensagem_util(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ErroTelemovel, match="adb não encontrado"):
        ca.adb_real("devices")


# ---------------------------------------------------- interface (smoke test)

def _bombear(raiz, app, segundos=10):
    fim = time.time() + segundos
    while time.time() < fim:
        raiz.update()
        if not app.ocupado and app.fila.empty():
            time.sleep(0.15)
            raiz.update()
            if not app.ocupado and app.fila.empty():
                return
        time.sleep(0.02)
    raise AssertionError("a interface não ficou livre a tempo")


@pytest.fixture
def gui(fake, tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        raiz = tk.Tk()
    except tk.TclError:
        pytest.skip("sem ecrã disponível para o Tk")
    telemovel = Telemovel(adb=fake, dormir=lambda _s: None)
    app = ca.App(
        raiz, telemovel=telemovel, config=ca.Config(tmp_path / "cfg.json"),
        pasta_raw=tmp_path / "raw", registo=Registo(tmp_path / "baixados.txt"))
    yield raiz, app, fake, tmp_path
    app.a_fechar = True
    app.executor.shutdown(wait=False, cancel_futures=True)
    raiz.destroy()


def texto_log(app) -> str:
    return app.txt.get("1.0", "end")


def test_gui_tirar_foto(gui):
    raiz, app, fake, tmp = gui
    app.var_x.set("500")
    app.var_y.set("600")
    app.tirar_foto()
    _bombear(raiz, app)
    assert "Foto guardada:" in texto_log(app)
    assert len(list((tmp / "raw").glob("*.jpg"))) == 1
    assert "shell input tap 500 600" in fake.comandos()
    assert str(app.btn_foto.cget("state")) == "normal"


def test_gui_valores_invalidos_nao_disparam(gui):
    raiz, app, fake, tmp = gui
    app.var_atraso.set("abc")
    app.tirar_foto()
    _bombear(raiz, app)
    assert "Valores inválidos" in texto_log(app)
    assert not list((tmp / "raw").glob("*"))


def test_gui_preview_e_clique_define_o_ponto_de_foco(gui):
    raiz, app, fake, tmp = gui
    app.atualizar_preview()
    _bombear(raiz, app, 20)
    assert app.escala == 5 and app.imagem_preview is not None
    assert int(app.canvas.cget("width")) == 216

    class Evento:
        x, y = 100, 200

    app.var_foco.set(False)
    app._clicar_preview(Evento)
    assert (app.var_x.get(), app.var_y.get()) == ("500", "1000")
    assert app.var_foco.get() is True
    assert app.canvas.find_withtag("marca")


def test_gui_manter_ligado_e_repor(gui):
    raiz, app, fake, tmp = gui
    app.manter_ligado()
    _bombear(raiz, app)
    assert fake.definicoes[("global", "stay_on_while_plugged_in")] == "7"
    assert app.var_auto.get() is True
    assert app.cfg.dados["energia_original"] == {"stay": "0", "timeout": "60000"}
    app.repor_energia()
    _bombear(raiz, app)
    assert fake.definicoes[("system", "screen_off_timeout")] == "60000"
    assert app.var_auto.get() is False
    assert "energia_original" not in app.cfg.dados
    assert "repostas" in texto_log(app)


def test_gui_abrir_camara(gui):
    raiz, app, fake, tmp = gui
    fake.camara_aberta = False
    app.abrir_camara()
    _bombear(raiz, app)
    assert "Câmara aberta." in texto_log(app)


def test_gui_erro_do_adb_aparece_no_log_e_liberta_os_botoes(gui):
    raiz, app, fake, tmp = gui
    fake.dispara = False
    app.tel.dormir = lambda _s: None
    app.tirar_foto()
    _bombear(raiz, app, 15)
    assert "ERRO (foto)" in texto_log(app)
    assert str(app.btn_foto.cget("state")) == "normal"


def test_gui_descarregar_novas_pede_confirmacao(gui, monkeypatch):
    raiz, app, fake, tmp = gui
    fake.adicionar("a.jpg", b"a", 1_700_000_000)
    fake.adicionar("b.jpg", b"b", 1_700_000_050)
    perguntas = []
    monkeypatch.setattr(ca.messagebox, "askyesno", lambda t, m: perguntas.append(m) or True)
    app.descarregar_novas()
    _bombear(raiz, app, 15)
    assert "2 foto(s)" in perguntas[0] and "APAGADA" not in perguntas[0]
    assert len(list((tmp / "raw").glob("*.jpg"))) == 2
    assert "Descarregadas 2 foto(s)." in texto_log(app)


def test_gui_recusar_confirmacao_nao_descarrega(gui, monkeypatch):
    raiz, app, fake, tmp = gui
    fake.adicionar("a.jpg")
    monkeypatch.setattr(ca.messagebox, "askyesno", lambda t, m: False)
    app.descarregar_novas()
    _bombear(raiz, app)
    assert not (tmp / "raw").exists() or not list((tmp / "raw").glob("*"))


def test_gui_aviso_de_apagar_na_confirmacao(gui, monkeypatch):
    raiz, app, fake, tmp = gui
    fake.adicionar("a.jpg")
    app.var_apagar.set(True)
    perguntas = []
    monkeypatch.setattr(ca.messagebox, "askyesno", lambda t, m: perguntas.append(m) or True)
    app.descarregar_novas()
    _bombear(raiz, app, 15)
    assert "APAGADA" in perguntas[0]
    assert fake.ficheiros == {}


def test_gui_ciclo_de_estado_reabre_camara_em_modo_automatico(gui):
    raiz, app, fake, tmp = gui
    fake.camara_aberta = False
    app.var_auto.set(True)
    app.estado_em_curso = True
    app.executor.submit(app._tarefa_estado, True)
    _bombear(raiz, app)
    raiz.update()
    assert fake.camara_aberta is True
    assert app.estado_vars["Câmara"].get() == "aberta"
    assert app.estado_vars["Telemóvel"].get() == "ligado (ABC123)"


def test_gui_estado_sem_adb_mostra_mensagem_uma_so_vez(gui):
    raiz, app, fake, tmp = gui
    app._mostrar_estado({"erro": "adb não encontrado"})
    app._mostrar_estado({"erro": "adb não encontrado"})
    assert texto_log(app).count("adb não encontrado") == 1
    assert app.estado_vars["Telemóvel"].get() == "sem ligação ao adb"


def test_gui_opcoes_persistem_mas_apagar_nunca(gui):
    raiz, app, fake, tmp = gui
    app.var_x.set("111")
    app.var_apagar.set(True)
    app._guardar_opcoes()
    guardado = ca.Config(tmp / "cfg.json").dados
    assert guardado["foco_xy"][0] == 111
    assert "apagar" not in guardado and "apagar" not in str(guardado)


# ------------------------------------------- descarregar TODAS / outro destino

def test_todas_ignora_as_que_ja_estao_no_destino(tel, fake, raw, registo, tmp_path):
    fake.adicionar("a.jpg", b"aaaa", 1_700_000_000)
    fake.adicionar("b.jpg", b"bbbbbb", 1_700_000_100)
    nomes = sorted(tel.listar())
    assert tel.descarregar_lista(nomes, raw, registo, ignorar_iguais=True) == (2, 0, [])
    # segunda passagem, mesmo com o registo perdido: nada é duplicado
    outro_registo = Registo(tmp_path / "outro.txt")
    assert tel.descarregar_lista(nomes, raw, outro_registo, ignorar_iguais=True) == (0, 2, [])
    assert len(list(raw.glob("*.jpg"))) == 2


def test_todas_recupera_o_que_foi_apagado_do_destino(tel, fake, raw, registo):
    fake.adicionar("a.jpg", b"aaaa", 1_700_000_000)
    tel.descarregar_lista(["a.jpg"], raw, registo, ignorar_iguais=True)
    next(raw.glob("*.jpg")).unlink()
    # o registo diz "já descarregada", mas a lista "todas" não depende dele
    assert tel.descarregar_lista(["a.jpg"], raw, registo, ignorar_iguais=True) == (1, 0, [])


def test_mesmo_nome_de_data_com_tamanho_diferente_nao_substitui(tel, fake, raw, registo):
    fake.adicionar("a.jpg", b"aaaa", 1_700_000_000)
    fake.adicionar("b.jpg", b"bbbbbb", 1_700_000_000)  # mesmo segundo, outro tamanho
    assert tel.descarregar_lista(["a.jpg", "b.jpg"], raw, registo, ignorar_iguais=True) == (2, 0, [])
    assert sorted(p.read_bytes() for p in raw.glob("*.jpg")) == [b"aaaa", b"bbbbbb"]


def test_ignoradas_nunca_sao_apagadas_do_telemovel(tel, fake, raw, registo):
    fake.adicionar("a.jpg", b"aaaa", 1_700_000_000)
    tel.descarregar_lista(["a.jpg"], raw, registo, ignorar_iguais=True)
    fake.adicionar("a.jpg", b"aaaa", 1_700_000_000)  # a mesma foto continua no telemóvel
    assert tel.descarregar_lista(["a.jpg"], raw, registo, apagar=True, ignorar_iguais=True) == (0, 1, [])
    assert "a.jpg" in fake.ficheiros


def test_outro_destino_recebe_copia_completa(tel, fake, raw, registo, tmp_path):
    fake.adicionar("a.jpg", b"aaaa", 1_700_000_000)
    tel.descarregar_lista(["a.jpg"], raw, registo, ignorar_iguais=True)
    outro = tmp_path / "dir novo" / "fotos"  # pasta que ainda não existe, com espaço no nome
    assert tel.descarregar_lista(["a.jpg"], outro, registo, ignorar_iguais=True) == (1, 0, [])
    assert next(outro.glob("*.jpg")).read_bytes() == b"aaaa"


def test_gui_todas_descarrega_mesmo_as_ja_registadas(gui, monkeypatch):
    raiz, app, fake, tmp = gui
    fake.adicionar("a.jpg", b"a", 1_700_000_000)
    app.registo.adicionar("a.jpg")
    perguntas = []
    monkeypatch.setattr(ca.messagebox, "askyesno", lambda t, m: perguntas.append(m) or True)
    app.descarregar_novas()
    _bombear(raiz, app)
    assert "Não há fotos novas" in texto_log(app) and not perguntas
    app.descarregar_todas()
    _bombear(raiz, app, 15)
    assert "TODAS as 1 foto(s)" in perguntas[0]
    assert len(list((tmp / "raw").glob("*.jpg"))) == 1
    app.descarregar_todas()
    _bombear(raiz, app, 15)
    assert "1 já existiam no destino" in texto_log(app)
    assert len(list((tmp / "raw").glob("*.jpg"))) == 1


def test_gui_destino_alterado_vale_para_fotos_e_descargas(gui, monkeypatch):
    raiz, app, fake, tmp = gui
    outro = tmp / "outra pasta"
    app.var_destino.set(str(outro))
    assert app.pasta_raw == outro
    app.var_foco.set(False)
    app.tirar_foto()
    _bombear(raiz, app)
    assert len(list(outro.glob("*.jpg"))) == 1 and not (tmp / "raw").exists()
    fake.adicionar("velha.jpg", b"v", 1_700_000_000)
    monkeypatch.setattr(ca.messagebox, "askyesno", lambda t, m: str(outro) in m)
    app.descarregar_todas()
    _bombear(raiz, app, 15)
    assert len(list(outro.glob("*.jpg"))) == 2
    app._guardar_opcoes()
    assert ca.Config(tmp / "cfg.json").dados["destino"] == str(outro)


def test_gui_escolher_destino(gui, monkeypatch):
    raiz, app, fake, tmp = gui
    monkeypatch.setattr(ca.filedialog, "askdirectory", lambda **kw: str(tmp / "escolhida"))
    app.escolher_destino()
    _bombear(raiz, app)
    assert app.var_destino.get() == str(tmp / "escolhida")
    monkeypatch.setattr(ca.filedialog, "askdirectory", lambda **kw: "")  # cancelado
    app.escolher_destino()
    assert app.var_destino.get() == str(tmp / "escolhida")


def test_gui_destino_vazio_volta_a_pasta_padrao(gui):
    raiz, app, fake, tmp = gui
    app.var_destino.set("   ")
    assert app.pasta_raw == tmp / "raw"


# ------------------------------------------------ CamPy Studio: marca e ficheiros

def test_nomes_dos_ficheiros_da_app():
    assert ca.FICHEIRO_CONFIG.name == "campy_studio_config.json"
    assert ca.FICHEIRO_REGISTO.name == "logs_img.txt"
    assert ca.PASTA_RAW.name == "raw"


def test_migracao_renomeia_ficheiros_antigos(tmp_path):
    (tmp_path / "camara_app_config.json").write_text('{"tecla": "X"}')
    (tmp_path / "camara_app_baixados.txt").write_text("IMG1.jpg\n")
    migrados = ca.migrar_ficheiros_antigos(tmp_path)
    assert len(migrados) == 2
    assert not (tmp_path / "camara_app_config.json").exists()
    assert ca.Config(tmp_path / "campy_studio_config.json").dados["tecla"] == "X"
    assert Registo(tmp_path / "logs_img.txt").nomes() == {"IMG1.jpg"}


def test_migracao_nunca_substitui_ficheiros_novos(tmp_path):
    (tmp_path / "camara_app_baixados.txt").write_text("antigo\n")
    (tmp_path / "logs_img.txt").write_text("novo\n")
    assert ca.migrar_ficheiros_antigos(tmp_path) == []
    assert (tmp_path / "logs_img.txt").read_text() == "novo\n"
    assert (tmp_path / "camara_app_baixados.txt").exists()


def test_migracao_sem_ficheiros_antigos_nao_faz_nada(tmp_path):
    assert ca.migrar_ficheiros_antigos(tmp_path) == []


def test_gui_titulo_e_rodape(gui):
    raiz, app, *_ = gui
    assert raiz.title() == "CamPy Studio"
    textos = []

    def varrer(w):
        for f in w.winfo_children():
            try:
                textos.append(str(f.cget("text")))
            except Exception:
                pass
            varrer(f)

    varrer(raiz)
    assert any("©" in t and ca.AUTOR in t for t in textos)
    assert any(t == "CamPy Studio" for t in textos)


def test_gui_logotipo_carrega_e_ausencia_nao_rebenta(gui, monkeypatch):
    raiz, app, *_ = gui
    assert app._carregar_logo(40) is not None  # assets/logo.png existe no repositório
    monkeypatch.setattr(ca, "caminho_recurso", lambda _r: Path("/nao/existe.png"))
    assert app._carregar_logo(40) is None


def test_gui_botoes_tem_cores(gui):
    from tkinter import ttk
    raiz, app, *_ = gui
    estilo = ttk.Style()
    assert estilo.lookup("Foto.TButton", "background") == ca.PALETA_BOTOES["Foto"][0]
    assert estilo.lookup("Sucesso.TButton", "background") == ca.PALETA_BOTOES["Sucesso"][0]
    assert str(app.btn_foto.cget("style")) == "Foto.TButton"
