#!/usr/bin/env python3
"""CamPy Studio: fotografia de produto com um telemóvel Android ligado por USB.

A aplicação dispara a câmara do telemóvel através do ADB (Android Debug Bridge)
e descarrega as fotos para o PC, na pasta ``raw/`` (ou noutra à escolha).

Funcionalidades principais:
  * tirar foto com foco por toque e descarregá-la logo para o PC;
  * nomear as fotos com a data e a hora (AAAAMMDD_HHMMSS), sem nunca substituir;
  * descarregar as fotos novas, ou todas, da câmara do telemóvel;
  * reabrir a câmara e manter o ecrã sempre ligado (e repor as definições);
  * pré-visualização do ecrã do telemóvel para escolher o ponto de foco.

Só usa a biblioteca padrão do Python (Tkinter) e o programa ``adb``.
O Pillow é opcional: se existir, o logótipo é redimensionado com mais qualidade.

Uso:  python campy_studio.py        (ou ``pythonw campy_studio.py``, sem consola)
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

# O Tkinter é importado de forma tolerante para que a lógica (classe Telemovel,
# funções de ficheiros, etc.) possa ser testada em máquinas sem ecrã/Tk.
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None  # type: ignore[assignment]

# O Pillow é opcional: serve apenas para redimensionar o logótipo com qualidade.
try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None  # type: ignore[assignment]


# ---------------------------------------------------------------- identidade

NOME_APP = "CamPy Studio"
VERSAO = "1.0.0"
AUTOR = "Moisés Rodrigues (MPRO)"
ANO = 2026
LICENCA = "MIT"

# ------------------------------------------------------------------- caminhos

# Quando a aplicação é empacotada (PyInstaller), os ficheiros do utilizador
# (raw/, configuração, registo) ficam ao lado do .exe e não na pasta temporária
# onde o PyInstaller descompacta o programa.
if getattr(sys, "frozen", False):
    PASTA_APP = Path(sys.executable).resolve().parent
else:
    PASTA_APP = Path(__file__).resolve().parent

PASTA_RAW = PASTA_APP / "raw"                               # destino por omissão das fotos
FICHEIRO_CONFIG = PASTA_APP / "campy_studio_config.json"    # opções guardadas entre sessões
FICHEIRO_REGISTO = PASTA_APP / "logs_img.txt"               # fotos do telemóvel já descarregadas

# Nomes usados pelas versões anteriores (app "camara_app"); são renomeados
# automaticamente no primeiro arranque para não se perderem definições nem registo.
FICHEIROS_ANTIGOS = {
    "camara_app_config.json": FICHEIRO_CONFIG.name,
    "camara_app_baixados.txt": FICHEIRO_REGISTO.name,
}

# ------------------------------------------------------------ parâmetros ADB

CAM_DIR = "/sdcard/DCIM/Camera"                        # pasta onde a câmara guarda as fotos
INTENT_CAMARA = "android.media.action.STILL_IMAGE_CAMERA"
TIMEOUT_ECRA_MS = 1_800_000                            # 30 min: tempo limite em "manter ligado"

# ------------------------------------------------------------------ aparência

LARGURA_PREVIEW = 230     # largura (px) da pré-visualização do ecrã do telemóvel
ALTURA_LOGO = 40          # altura (px) do logótipo no cabeçalho

COR_CABECALHO = "#1b2a41"
COR_FAIXA = "#1971c2"
COR_SUBTITULO = "#adb5bd"
COR_RODAPE = "#e9ecef"
COR_TEXTO_RODAPE = "#495057"

# Cores dos botões: nome do estilo -> (normal, ao passar o rato, texto).
PALETA_BOTOES = {
    "Foto": ("#c92a2a", "#a61e1e", "#ffffff"),       # ação principal: tirar foto
    "Sucesso": ("#2b8a3e", "#237032", "#ffffff"),    # descarregar fotos
    "Info": ("#1971c2", "#145a9c", "#ffffff"),       # abrir câmara / pré-visualização
    "Aviso": ("#f08c00", "#d97d00", "#1a1a1a"),      # manter ecrã e câmara ligados
    "Neutro": ("#495057", "#343a40", "#ffffff"),     # ações secundárias
}


class ErroTelemovel(Exception):
    """Falha ao falar com o telemóvel (a mensagem já está pronta para mostrar)."""


# ---------------------------------------------------------------------- ADB

def adb_real(*args: str, binario: bool = False, timeout: float = 60):
    """Executa ``adb <args>`` e devolve a saída (texto, ou bytes se ``binario``).

    Lança ErroTelemovel com uma mensagem clara se o adb não existir, não
    responder a tempo ou terminar com erro.
    """
    try:
        r = subprocess.run(
            ["adb", *args],
            capture_output=True,
            timeout=timeout,
            # No Windows, evita que apareça uma janela de consola a cada comando.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as e:
        raise ErroTelemovel(
            "adb não encontrado: instala as platform-tools e reabre o terminal."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ErroTelemovel(f"O adb não respondeu em {timeout:g}s ({' '.join(args[:3])}).") from e
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise ErroTelemovel(msg or f"O adb devolveu o código {r.returncode}.")
    return r.stdout if binario else r.stdout.decode("utf-8", "replace")


# -------------------------------------------------------------- ficheiros no PC

def reservar(pasta: Path, base: str, ext: str = ".jpg") -> Path:
    """Cria (vazio) um ficheiro com um nome livre e devolve o seu caminho.

    Tenta ``base.jpg``, depois ``base_1.jpg``, ``base_2.jpg``... A criação em
    modo exclusivo ("x") garante que nunca se substitui um ficheiro existente.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        destino = pasta / (f"{base}{ext}" if n == 0 else f"{base}_{n}{ext}")
        try:
            with destino.open("xb"):
                pass
            return destino
        except FileExistsError:
            n += 1


def ja_existe(pasta: Path, base: str, tamanho: int) -> bool:
    """Indica se já há em ``pasta`` uma cópia (base.jpg, base_1.jpg...) com este tamanho."""
    return any(p.stat().st_size == tamanho for p in pasta.glob(f"{base}*.jpg"))


class Registo:
    """Lista, em ficheiro de texto, das fotos do telemóvel já descarregadas.

    Guarda o nome que a foto tem no telemóvel (um por linha), para que o botão
    "Descarregar novas" não repita transferências.
    """

    def __init__(self, ficheiro: Path = FICHEIRO_REGISTO):
        self.ficheiro = ficheiro

    def nomes(self) -> set[str]:
        """Devolve os nomes já registados (conjunto vazio se o ficheiro não existir)."""
        try:
            return set(self.ficheiro.read_text("utf-8").splitlines())
        except FileNotFoundError:
            return set()

    def adicionar(self, nome: str) -> None:
        """Acrescenta um nome ao fim do ficheiro."""
        self.ficheiro.parent.mkdir(parents=True, exist_ok=True)
        with self.ficheiro.open("a", encoding="utf-8") as f:
            f.write(nome + "\n")


class Config:
    """Opções da aplicação guardadas em JSON entre sessões."""

    def __init__(self, ficheiro: Path = FICHEIRO_CONFIG):
        self.ficheiro = ficheiro
        try:
            self.dados: dict = json.loads(ficheiro.read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            self.dados = {}  # sem ficheiro (ou inválido): começa com valores por omissão

    def guardar(self) -> None:
        self.ficheiro.write_text(json.dumps(self.dados, indent=2), "utf-8")


def migrar_ficheiros_antigos(pasta: Path = PASTA_APP) -> list[str]:
    """Renomeia os ficheiros da versão anterior para os novos nomes.

    Só renomeia quando o ficheiro novo ainda não existe, para nunca perder dados.
    Devolve uma descrição de cada ficheiro migrado.
    """
    migrados = []
    for antigo, novo in FICHEIROS_ANTIGOS.items():
        origem, destino = pasta / antigo, pasta / novo
        if origem.exists() and not destino.exists():
            origem.rename(destino)
            migrados.append(f"{antigo} -> {novo}")
    return migrados


def caminho_recurso(relativo: str) -> Path:
    """Localiza um ficheiro de dados da aplicação (por exemplo, assets/logo.png).

    Funciona a correr como script e dentro de um executável do PyInstaller,
    que descompacta os recursos numa pasta temporária (``sys._MEIPASS``).
    """
    base = Path(getattr(sys, "_MEIPASS", PASTA_APP))
    return base / relativo


def descrever_energia(energia: dict) -> str:
    """Resume em texto as definições de energia lidas do telemóvel."""
    stay, tout = energia.get("stay"), energia.get("timeout")
    minutos = f"{int(tout) / 60000:g} min" if tout and tout.isdigit() else "?"
    if stay not in (None, "0"):
        return f"ecrã sempre ligado a carregar (tempo limite {minutos})"
    return f"normal (o ecrã desliga em {minutos})"


# -------------------------------------------------------------------- telemóvel

class Telemovel:
    """Tudo o que a aplicação faz ao telemóvel, sempre através de comandos ADB.

    Os comandos ADB, a espera e o relógio são injetáveis, o que permite testar
    esta classe com um telemóvel simulado, sem hardware nem esperas reais.
    """

    def __init__(
        self,
        adb: Callable = adb_real,
        dormir: Callable[[float], None] = time.sleep,
        agora: Callable[[], datetime] = datetime.now,
        cam_dir: str = CAM_DIR,
    ):
        self._adb = adb
        self.dormir = dormir
        self.agora = agora
        self.cam_dir = cam_dir

    def sh(self, *args: str, timeout: float = 60) -> str:
        """Executa um comando na shell do telemóvel (``adb shell ...``)."""
        return self._adb("shell", *args, timeout=timeout)

    # -- estado do telemóvel

    def ligacao(self) -> tuple[str | None, str]:
        """Devolve (número de série, estado) do primeiro dispositivo listado pelo adb.

        O estado é "device" (pronto), "unauthorized" (falta aceitar no telemóvel)
        ou "sem dispositivo".
        """
        for linha in self._adb("devices").splitlines()[1:]:
            p = linha.split()
            if len(p) >= 2:
                return p[0], p[1]
        return None, "sem dispositivo"

    def ecra_ligado(self) -> bool:
        """True se o ecrã está aceso (e não em suspensão)."""
        try:
            return "Awake" in self.sh("dumpsys power | grep mWakefulness=")
        except ErroTelemovel:
            return False

    def camara_em_primeiro_plano(self) -> bool:
        """True se a janela em foco no telemóvel é a da app Câmara."""
        try:
            saida = self.sh("dumpsys window | grep mCurrentFocus")
        except ErroTelemovel:
            return False  # sem janela em foco (ecrã apagado, por exemplo)
        return "camera" in saida.lower()

    def _definicao(self, espaco: str, nome: str) -> str | None:
        """Lê uma definição do Android; devolve None se não estiver definida."""
        valor = self.sh("settings", "get", espaco, nome).strip()
        return None if valor in ("", "null") else valor

    def energia(self) -> dict:
        """Lê as duas definições de energia que a aplicação sabe alterar."""
        return {
            "stay": self._definicao("global", "stay_on_while_plugged_in"),
            "timeout": self._definicao("system", "screen_off_timeout"),
        }

    def estado(self) -> dict:
        """Junta tudo o que se mostra no painel "Estado"."""
        serial, ligacao = self.ligacao()
        est = {"serial": serial, "ligacao": ligacao, "ecra": False, "camara": False, "energia": {}}
        if ligacao == "device":  # só pergunta mais coisas se o telemóvel estiver pronto
            est["ecra"] = self.ecra_ligado()
            est["camara"] = self.camara_em_primeiro_plano()
            est["energia"] = self.energia()
        return est

    # -- câmara

    def abrir_camara(self) -> bool:
        """Acorda o ecrã e abre a app Câmara. Devolve True se a câmara ficou em primeiro plano."""
        self.sh("input", "keyevent", "KEYCODE_WAKEUP")
        try:
            self.sh("wm", "dismiss-keyguard")
        except ErroTelemovel:
            pass  # só funciona sem PIN; com PIN, desbloqueia-se à mão
        self.sh("am", "start", "-a", INTENT_CAMARA)
        for _ in range(8):  # até ~4 s à espera de a câmara aparecer
            self.dormir(0.5)
            if self.camara_em_primeiro_plano():
                return True
        return False

    def garantir_camara(self) -> None:
        """Abre a câmara se ainda não estiver aberta; falha com uma mensagem clara se não conseguir."""
        if self.camara_em_primeiro_plano():
            return
        if not self.abrir_camara():
            raise ErroTelemovel(
                "Não consegui abrir a câmara. Desbloqueia o telemóvel (PIN) e tenta de novo."
            )
        self.dormir(1.5)  # dá tempo à câmara para arrancar e focar

    # -- energia

    def manter_ligado(self, original: dict | None) -> dict:
        """Mantém o ecrã sempre ligado (com USB/carregador) e a câmara aberta.

        ``original`` são os valores de energia guardados antes da primeira
        alteração; se for None, lêem-se agora. Devolve esses valores originais,
        que a aplicação guarda para o botão "Repor energia normal".
        """
        if original is None:
            original = self.energia()
        self.sh("svc", "power", "stayon", "true")  # ecrã aceso enquanto houver cabo/carregador
        self.sh("settings", "put", "system", "screen_off_timeout", str(TIMEOUT_ECRA_MS))
        self.abrir_camara()
        return original

    def repor_energia(self, original: dict | None) -> bool:
        """Repõe as definições de energia. Devolve True se havia valores originais guardados."""
        stay = (original or {}).get("stay")
        if stay in (None, "0"):
            self.sh("svc", "power", "stayon", "false")
        else:
            self.sh("settings", "put", "global", "stay_on_while_plugged_in", stay)
        timeout = (original or {}).get("timeout")
        if timeout:  # sem valor guardado, o tempo limite do ecrã fica como está
            self.sh("settings", "put", "system", "screen_off_timeout", timeout)
        return original is not None

    # -- fotos

    def listar(self) -> list[str]:
        """Nomes das fotos JPEG na pasta da câmara (ignora vídeos e ficheiros temporários)."""
        saida = self.sh("ls", "-1", self.cam_dir)
        nomes = [n.strip() for n in saida.splitlines()]
        return [n for n in nomes if n.lower().endswith((".jpg", ".jpeg")) and not n.startswith(".")]

    def _stat(self, formato: str, nome: str) -> int | None:
        """Lê um número do ``stat`` de uma foto (``%s`` = tamanho, ``%Y`` = data em segundos).

        O caminho vai entre aspas porque os nomes podem ter parênteses, que a
        shell do telemóvel interpretaria como sintaxe.
        """
        try:
            return int(self.sh("stat", "-c", formato, shlex.quote(f"{self.cam_dir}/{nome}")).strip())
        except (ErroTelemovel, ValueError):
            return None

    def _aguardar_estavel(self, nome: str) -> None:
        """Espera que a câmara acabe de gravar a foto (tamanho estável)."""
        atual = self._stat("%s", nome)
        if atual is None:  # sem stat disponível: espera um tempo fixo
            self.dormir(1.0)
            return
        for _ in range(10):
            self.dormir(0.4)
            seguinte = self._stat("%s", nome)
            if seguinte == atual:
                return
            if seguinte is not None:
                atual = seguinte

    def _esperar_nova(self, antes: set[str], timeout: float) -> str:
        """Espera que apareça uma foto que não estava em ``antes`` e devolve o seu nome."""
        for _ in range(max(1, int(timeout / 0.5))):
            self.dormir(0.5)
            novos = sorted(set(self.listar()) - antes)
            if novos:
                self._aguardar_estavel(novos[-1])
                return novos[-1]
        raise ErroTelemovel(
            "A câmara não gravou nenhuma foto nova. Confirma que a app Câmara está aberta "
            "(modo Foto) e que a tecla de disparo está certa."
        )

    def descarregar(self, nome: str, pasta: Path, registo: Registo, apagar: bool = False,
                    ignorar_iguais: bool = False) -> Path | None:
        """Descarrega uma foto para ``pasta``, com o nome AAAAMMDD_HHMMSS.jpg.

        A data e a hora são as da própria foto no telemóvel. Se o nome já
        existir, acrescenta _1, _2... e nunca substitui nada.

        Com ``ignorar_iguais``, devolve None (sem descarregar nem apagar) quando
        já existe em ``pasta`` uma cópia com a mesma data/hora e tamanho.
        Com ``apagar``, remove a foto do telemóvel, mas só depois de confirmar
        que o ficheiro copiado tem o tamanho certo.
        """
        remoto = f"{self.cam_dir}/{nome}"
        tamanho = self._stat("%s", nome)
        epoch = self._stat("%Y", nome)
        data = datetime.fromtimestamp(epoch) if epoch else self.agora()
        base = data.strftime("%Y%m%d_%H%M%S")
        if ignorar_iguais and tamanho is not None and ja_existe(pasta, base, tamanho):
            registo.adicionar(nome)
            return None
        destino = reservar(pasta, base)
        try:
            self._adb("pull", remoto, str(destino), timeout=120)
            if tamanho is not None and destino.stat().st_size != tamanho:
                raise ErroTelemovel(f"Transferência incompleta de {nome}; a foto ficou no telemóvel.")
        except Exception:
            destino.unlink(missing_ok=True)  # não deixar ficheiros vazios ou a meio
            raise
        registo.adicionar(nome)
        if apagar:  # um ficheiro de cada vez, nunca de forma recursiva
            self.sh("rm", shlex.quote(remoto))
        return destino

    def tirar_foto(
        self,
        pasta: Path,
        registo: Registo,
        *,
        foco: tuple[int, int] | None = None,
        espera_foco: float = 1.5,
        atraso: float = 1.0,
        tecla: str = "KEYCODE_CAMERA",
        apagar: bool = False,
        timeout: float = 20.0,
    ) -> Path:
        """Tira uma foto e descarrega-a para ``pasta``; devolve o caminho do ficheiro.

        Passos: garantir a câmara aberta, tocar no ponto de foco (opcional),
        esperar, disparar e esperar que a foto nova apareça no telemóvel.
        """
        self.garantir_camara()
        if foco:
            self.sh("input", "tap", str(foco[0]), str(foco[1]))
            self.dormir(espera_foco)  # tempo para a câmara focar
        self.dormir(atraso)           # deixa assentar qualquer vibração
        antes = set(self.listar())
        self.sh("input", "keyevent", tecla)
        nome = self._esperar_nova(antes, timeout)
        return self.descarregar(nome, pasta, registo, apagar)

    def novas(self, registo: Registo) -> list[str]:
        """Fotos do telemóvel que ainda não constam do registo de descarregadas."""
        feitas = registo.nomes()
        return sorted(n for n in self.listar() if n not in feitas)

    def descarregar_lista(
        self,
        nomes: list[str],
        pasta: Path,
        registo: Registo,
        apagar: bool = False,
        log: Callable[[str], None] = lambda _m: None,
        ignorar_iguais: bool = False,
    ) -> tuple[int, int, list[str]]:
        """Descarrega várias fotos. Devolve (descarregadas, ignoradas, erros).

        Uma foto que falhe não interrompe as restantes: o erro fica na lista.
        """
        feitas, ignoradas, erros = 0, 0, []
        for i, nome in enumerate(nomes, 1):
            try:
                destino = self.descarregar(nome, pasta, registo, apagar, ignorar_iguais)
            except ErroTelemovel as e:
                erros.append(f"{nome}: {e}")
                continue
            if destino is None:
                ignoradas += 1
                log(f"  {i}/{len(nomes)}  {nome}: já existe no destino, ignorada")
            else:
                feitas += 1
                log(f"  {i}/{len(nomes)}  {destino.name}")
        return feitas, ignoradas, erros

    def captura_ecra(self) -> bytes:
        """Captura do ecrã do telemóvel em PNG (usada na pré-visualização)."""
        return self._adb("exec-out", "screencap", "-p", binario=True, timeout=30)


def abrir_pasta(pasta: Path) -> None:
    """Abre a pasta no explorador de ficheiros do sistema (criando-a se for preciso)."""
    pasta.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(pasta)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(pasta)], check=False)
    else:
        subprocess.run(["xdg-open", str(pasta)], check=False)


def aplicar_estilos(estilo: "ttk.Style") -> None:
    """Define o tema e os botões coloridos.

    O tema "clam" é necessário porque o tema nativo do Windows ignora as cores
    de fundo dos botões do ttk.
    """
    try:
        estilo.theme_use("clam")
    except tk.TclError:
        pass  # tema indisponível: ficam os botões sem cor, mas a app funciona
    for nome, (normal, ao_passar, texto) in PALETA_BOTOES.items():
        # A ação principal ("Foto") tem um botão maior do que os restantes.
        fonte = ("Segoe UI", 14, "bold") if nome == "Foto" else ("Segoe UI", 10, "bold")
        folga = 7 if nome == "Foto" else (8, 5)
        estilo.configure(
            f"{nome}.TButton", background=normal, foreground=texto, font=fonte,
            padding=folga, borderwidth=0, focuscolor=normal,
        )
        estilo.map(
            f"{nome}.TButton",
            background=[("disabled", "#ced4da"), ("pressed", ao_passar), ("active", ao_passar)],
            foreground=[("disabled", "#868e96")],
        )


# ------------------------------------------------------------------ interface

class App:
    """Janela principal.

    Os comandos ADB demoram (ou podem falhar), por isso correm todos numa
    thread de trabalho, um de cada vez. A thread da interface nunca bloqueia:
    os resultados voltam por uma fila (``self.fila``) que a interface esvazia
    de 100 em 100 ms. O Tkinter só pode ser usado a partir da sua própria thread.
    """

    def __init__(
        self,
        raiz: "tk.Tk",
        telemovel: Telemovel | None = None,
        config: Config | None = None,
        pasta_raw: Path = PASTA_RAW,
        registo: Registo | None = None,
    ):
        self.raiz = raiz
        self.tel = telemovel or Telemovel()
        self.cfg = config or Config()
        self.pasta_padrao = pasta_raw
        self.registo = registo or Registo()
        self.executor = ThreadPoolExecutor(max_workers=1)  # um comando ADB de cada vez
        self.fila: queue.Queue[Callable[[], None]] = queue.Queue()  # thread de trabalho -> interface
        self.ocupado = 0                 # nº de tarefas em curso (desativa os botões)
        self.estado_em_curso = False     # há uma leitura periódica do estado a decorrer
        self.a_fechar = False
        self.botoes: list = []           # botões que ficam inativos enquanto há trabalho
        self.imagem_preview = None
        self.escala = 4                  # fator de redução da pré-visualização
        self.logo = None
        self._ultimo_erro: str | None = None
        self._construir()
        self.raiz.protocol("WM_DELETE_WINDOW", self._fechar)
        self.raiz.bind("<F2>", lambda _e: self.tirar_foto())  # atalho para tirar foto
        self.raiz.after(100, self._drenar)
        self.raiz.after(500, self._ciclo_estado)
        self.log(f"Pronto. As fotos vão para {self.pasta_raw}")

    @property
    def pasta_raw(self) -> Path:
        """Pasta de destino atual (campo "Destino"; por omissão, raw/)."""
        texto = self.var_destino.get().strip()
        return Path(texto) if texto else self.pasta_padrao

    # -- construção da janela

    def _carregar_logo(self, altura: int):
        """Carrega assets/logo.png (versão clara, para o cabeçalho escuro) redimensionado para ``altura`` px; None se não existir."""
        ficheiro = caminho_recurso("assets/logo.png")
        if not ficheiro.exists():
            return None
        try:
            if Image is not None:  # com Pillow: redimensionamento suave
                img = Image.open(ficheiro).convert("RGBA")
                largura = max(1, round(img.width * altura / img.height))
                return ImageTk.PhotoImage(img.resize((largura, altura), Image.Resampling.LANCZOS))
            # Sem Pillow, o Tk só reduz por fatores inteiros (pode ficar menos suave).
            original = tk.PhotoImage(file=str(ficheiro))
            fator = max(1, -(-original.height() // altura))  # divisão por excesso
            return original.subsample(fator, fator)
        except (OSError, tk.TclError):
            return None  # logótipo ilegível: a aplicação funciona na mesma

    def _construir_cabecalho(self) -> None:
        r = self.raiz
        cabecalho = tk.Frame(r, bg=COR_CABECALHO)
        cabecalho.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.logo = self._carregar_logo(ALTURA_LOGO)
        if self.logo is not None:
            tk.Label(cabecalho, image=self.logo, bg=COR_CABECALHO).pack(side="left", padx=(12, 8), pady=4)
        try:  # ícone da janela e da barra de tarefas (o formato .ico só é aceite no Windows)
            if sys.platform.startswith("win"):
                r.iconbitmap(str(caminho_recurso("assets/logo.ico")))
        except tk.TclError:
            pass  # sem ícone, a janela funciona na mesma
        textos = tk.Frame(cabecalho, bg=COR_CABECALHO)
        textos.pack(side="left", pady=4, padx=(0 if self.logo else 12, 0))
        tk.Label(textos, text=NOME_APP, font=("Segoe UI", 16, "bold"),
                 fg="#ffffff", bg=COR_CABECALHO).pack(anchor="w")
        tk.Label(textos, text="Fotografia de produto com o telemóvel Android",
                 font=("Segoe UI", 9), fg=COR_SUBTITULO, bg=COR_CABECALHO).pack(anchor="w")
        # Faixa colorida fina por baixo do cabeçalho.
        tk.Frame(r, bg=COR_FAIXA, height=3).grid(row=1, column=0, columnspan=2, sticky="ew")

    def _construir_rodape(self) -> None:
        rodape = tk.Frame(self.raiz, bg=COR_RODAPE)
        rodape.grid(row=4, column=0, columnspan=2, sticky="ew")
        tk.Label(rodape, text=f"{NOME_APP} v{VERSAO}  ·  Python + Tkinter + ADB",
                 font=("Segoe UI", 8), fg=COR_TEXTO_RODAPE, bg=COR_RODAPE).pack(side="left", padx=10, pady=3)
        tk.Label(rodape, text=f"© {ANO} {AUTOR}  ·  Licença {LICENCA}",
                 font=("Segoe UI", 8), fg=COR_TEXTO_RODAPE, bg=COR_RODAPE).pack(side="right", padx=10, pady=3)

    def _construir(self) -> None:
        r, c = self.raiz, self.cfg.dados
        r.title(NOME_APP)
        r.minsize(740, 560)
        aplicar_estilos(ttk.Style())

        # Variáveis ligadas aos campos; os valores iniciais vêm da configuração guardada.
        x, y = c.get("foco_xy", [540, 700])
        self.var_foco = tk.BooleanVar(value=c.get("foco_ativo", True))
        self.var_x = tk.StringVar(value=str(x))
        self.var_y = tk.StringVar(value=str(y))
        self.var_espera = tk.StringVar(value=str(c.get("espera_foco", 1.5)))
        self.var_atraso = tk.StringVar(value=str(c.get("atraso", 1.0)))
        self.var_tecla = tk.StringVar(value=c.get("tecla", "KEYCODE_CAMERA"))
        self.var_apagar = tk.BooleanVar(value=False)  # nunca é guardado, por segurança
        self.var_auto = tk.BooleanVar(value=c.get("auto_camara", False))
        self.var_destino = tk.StringVar(value=c.get("destino", str(self.pasta_padrao)))

        self._construir_cabecalho()

        # Corpo: coluna da esquerda (controlos) e da direita (pré-visualização).
        esq = ttk.Frame(r, padding=10)
        esq.grid(row=2, column=0, sticky="nsew")
        dire = ttk.Frame(r, padding=(0, 10, 10, 10))
        dire.grid(row=2, column=1, sticky="n")
        r.columnconfigure(0, weight=1)
        r.rowconfigure(3, weight=1)  # o registo de mensagens é a zona que estica

        # Estado do telemóvel (atualizado de 5 em 5 segundos).
        caixa = ttk.LabelFrame(esq, text="Estado", padding=8)
        caixa.pack(fill="x")
        self.estado_vars: dict[str, tk.StringVar] = {}
        # Disposição compacta: Telemóvel / Ecrã + Câmara / Energia (3 linhas).
        posicoes = {"Telemóvel": (0, 0), "Ecrã": (1, 0), "Câmara": (1, 2), "Energia": (2, 0)}
        for nome, (linha, coluna) in posicoes.items():
            ttk.Label(caixa, text=f"{nome}:").grid(row=linha, column=coluna, sticky="w")
            v = tk.StringVar(value="—")
            ttk.Label(caixa, textvariable=v).grid(row=linha, column=coluna + 1, sticky="w", padx=6)
            self.estado_vars[nome] = v

        self.btn_foto = ttk.Button(esq, text="Tirar foto  (F2)", style="Foto.TButton", command=self.tirar_foto)
        self.btn_foto.pack(fill="x", pady=8)

        # Foco e disparo.
        foco = ttk.LabelFrame(esq, text="Foco e disparo", padding=8)
        foco.pack(fill="x")
        ttk.Checkbutton(foco, text="Focar por toque antes de disparar", variable=self.var_foco).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(foco, text="Ponto de foco X, Y:").grid(row=1, column=0, sticky="w")
        ttk.Entry(foco, textvariable=self.var_x, width=6).grid(row=1, column=1, padx=2)
        ttk.Entry(foco, textvariable=self.var_y, width=6).grid(row=1, column=2, padx=2)
        ttk.Label(foco, text="(clica na imagem à direita)").grid(row=1, column=3, sticky="w", padx=6)
        ttk.Label(foco, text="Espera após o foco (s):").grid(row=2, column=0, sticky="w")
        ttk.Entry(foco, textvariable=self.var_espera, width=6).grid(row=2, column=1, padx=2)
        atraso = ttk.Frame(foco)
        atraso.grid(row=2, column=2, columnspan=2, sticky="w", padx=(6, 0))
        ttk.Label(atraso, text="Atraso (s):").pack(side="left")
        ttk.Entry(atraso, textvariable=self.var_atraso, width=6).pack(side="left", padx=4)
        ttk.Label(foco, text="Tecla de disparo:").grid(row=3, column=0, sticky="w")
        ttk.Entry(foco, textvariable=self.var_tecla, width=20).grid(row=3, column=1, columnspan=3, sticky="w", padx=2)

        # Fotos: destino e descargas.
        fotos = ttk.LabelFrame(esq, text="Fotos", padding=8)
        fotos.pack(fill="x", pady=(8, 0))
        destino = ttk.Frame(fotos)
        destino.pack(fill="x", pady=(0, 4))
        ttk.Label(destino, text="Destino:").pack(side="left")
        ttk.Entry(destino, textvariable=self.var_destino).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(destino, text="Escolher...", style="Neutro.TButton",
                   command=self.escolher_destino).pack(side="left")
        ttk.Button(destino, text="Abrir", width=6, style="Neutro.TButton",
                   command=lambda: abrir_pasta(self.pasta_raw)).pack(side="left", padx=(2, 0))
        linha = ttk.Frame(fotos)
        linha.pack(fill="x")
        linha.columnconfigure((0, 1), weight=1, uniform="descarga")  # dois botões com a mesma largura
        self.btn_novas = ttk.Button(linha, text="Descarregar novas", style="Sucesso.TButton",
                                    command=self.descarregar_novas)
        self.btn_novas.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self.btn_todas = ttk.Button(linha, text="Descarregar todas", style="Sucesso.TButton",
                                    command=self.descarregar_todas)
        self.btn_todas.grid(row=0, column=1, sticky="ew", padx=(2, 0))
        ttk.Checkbutton(fotos, text="Apagar do telemóvel depois de descarregar",
                        variable=self.var_apagar).pack(anchor="w", pady=(4, 0))

        # Telemóvel: câmara e energia.
        tel = ttk.LabelFrame(esq, text="Telemóvel", padding=8)
        tel.pack(fill="x", pady=(8, 0))
        tel.columnconfigure((0, 1), weight=1, uniform="tel")
        self.btn_camara = ttk.Button(tel, text="Abrir câmara", style="Info.TButton", command=self.abrir_camara)
        self.btn_camara.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self.btn_repor = ttk.Button(tel, text="Repor energia normal", style="Neutro.TButton",
                                    command=self.repor_energia)
        self.btn_repor.grid(row=0, column=1, sticky="ew", padx=(2, 0))
        self.btn_manter = ttk.Button(tel, text="Manter ecrã e câmara sempre ligados", style="Aviso.TButton",
                                     command=self.manter_ligado)
        self.btn_manter.grid(row=1, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Checkbutton(tel, text="Reabrir a câmara automaticamente se fechar",
                        variable=self.var_auto).grid(row=2, column=0, columnspan=2, sticky="w")

        # Pré-visualização do ecrã do telemóvel (clicar nela define o ponto de foco).
        self.canvas = tk.Canvas(dire, width=LARGURA_PREVIEW, height=470, bg="#d9d9d9", highlightthickness=1)
        self.canvas.pack()
        self.canvas.create_text(LARGURA_PREVIEW // 2, 235, text="Pré-visualização do ecrã\ndo telemóvel",
                                justify="center")
        self.canvas.bind("<Button-1>", self._clicar_preview)
        self.btn_preview = ttk.Button(dire, text="Atualizar pré-visualização", style="Info.TButton",
                                      command=self.atualizar_preview)
        self.btn_preview.pack(fill="x", pady=(6, 0))

        # Registo de mensagens.
        logf = ttk.Frame(r, padding=(10, 0, 10, 8))
        logf.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self.txt = tk.Text(logf, height=3, state="disabled", wrap="word")
        barra = ttk.Scrollbar(logf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        self._construir_rodape()

        self.botoes = [self.btn_foto, self.btn_novas, self.btn_todas, self.btn_camara,
                       self.btn_manter, self.btn_repor, self.btn_preview]

    # -- infraestrutura: fila de mensagens, registo e tarefas em segundo plano

    def _ui(self, fn: Callable[[], None]) -> None:
        """Pede à thread da interface que execute ``fn`` (seguro a partir de qualquer thread)."""
        self.fila.put(fn)

    def _drenar(self) -> None:
        """Executa, na thread da interface, o que as outras threads colocaram na fila."""
        while True:
            try:
                fn = self.fila.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:  # um erro numa função nunca pode matar o ciclo da interface
                self._escrever(f"{datetime.now():%H:%M:%S}  ERRO interno: {e!r}\n")
        if not self.a_fechar:
            self.raiz.after(100, self._drenar)

    def log(self, msg: str) -> None:
        """Escreve uma linha com a hora no registo de mensagens (pode ser chamado de qualquer thread)."""
        linha = f"{datetime.now():%H:%M:%S}  {msg}\n"
        self._ui(lambda: self._escrever(linha))

    def _escrever(self, linha: str) -> None:
        self.txt.configure(state="normal")  # o campo é só de leitura, exceto aqui
        self.txt.insert("end", linha)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _estado_botoes(self) -> None:
        """Desativa os botões enquanto há uma tarefa em curso."""
        estado = "disabled" if self.ocupado else "normal"
        for b in self.botoes:
            b.configure(state=estado)

    def executar(self, descricao: str, fn: Callable[[], object],
                 ao_terminar: Callable[[object], None] | None = None) -> None:
        """Corre ``fn`` na thread de trabalho.

        Se correr bem, chama ``ao_terminar(resultado)`` na thread da interface.
        Se falhar, escreve o erro no registo e os botões voltam a ficar ativos.
        """
        self.ocupado += 1
        self._estado_botoes()

        def tarefa() -> None:
            ok, resultado = True, None
            try:
                resultado = fn()
            except ErroTelemovel as e:
                ok = False
                self.log(f"ERRO ({descricao}): {e}")
            except Exception as e:
                ok = False
                self.log(f"ERRO inesperado ({descricao}): {e!r}")

            def fim() -> None:
                self.ocupado -= 1
                self._estado_botoes()
                if ok and ao_terminar:
                    ao_terminar(resultado)

            self._ui(fim)

        self.executor.submit(tarefa)

    def _opcoes(self) -> dict:
        """Lê e valida as opções de foco/disparo (na thread da interface)."""
        try:
            foco = (int(self.var_x.get()), int(self.var_y.get())) if self.var_foco.get() else None
            espera = float(self.var_espera.get().replace(",", "."))
            atraso = float(self.var_atraso.get().replace(",", "."))
        except ValueError as e:
            raise ErroTelemovel("Valores inválidos nas opções de foco/atraso.") from e
        return {
            "foco": foco,
            "espera_foco": espera,
            "atraso": atraso,
            "tecla": self.var_tecla.get().strip() or "KEYCODE_CAMERA",
            "apagar": self.var_apagar.get(),
        }

    def _guardar_opcoes(self) -> None:
        """Guarda as opções no ficheiro de configuração (exceto "apagar", por segurança)."""
        d = self.cfg.dados
        try:
            d["foco_xy"] = [int(self.var_x.get()), int(self.var_y.get())]
        except ValueError:
            pass  # valor inválido: mantém o anterior
        d["foco_ativo"] = self.var_foco.get()
        d["espera_foco"] = self.var_espera.get()
        d["atraso"] = self.var_atraso.get()
        d["tecla"] = self.var_tecla.get().strip() or "KEYCODE_CAMERA"
        d["auto_camara"] = self.var_auto.get()
        d["destino"] = self.var_destino.get().strip()
        self.cfg.guardar()

    # -- ações dos botões

    def tirar_foto(self) -> None:
        """Tira uma foto e descarrega-a para a pasta de destino."""
        if self.ocupado:
            return  # já há um comando em curso (por exemplo, atalho F2 repetido)
        try:
            op = self._opcoes()
        except ErroTelemovel as e:
            self.log(str(e))
            return
        self._guardar_opcoes()
        destino_pasta = self.pasta_raw  # lido aqui, na thread da interface (o Tk não é thread-safe)
        self.log("A tirar foto...")
        self.executar(
            "foto",
            lambda: self.tel.tirar_foto(destino_pasta, self.registo, **op),
            lambda destino: self.log(f"Foto guardada: {destino.name}"),
        )

    def descarregar_novas(self) -> None:
        """Descarrega só as fotos que ainda não estão no registo."""
        self._descarregar_fotos(todas=False)

    def descarregar_todas(self) -> None:
        """Descarrega todas as fotos da câmara (as que já existem no destino são ignoradas)."""
        self._descarregar_fotos(todas=True)

    def _descarregar_fotos(self, todas: bool) -> None:
        # Duas fases: primeiro conta as fotos (thread de trabalho); depois pede
        # confirmação ao utilizador (thread da interface) e só então descarrega.
        apagar = self.var_apagar.get()
        destino = self.pasta_raw  # lido aqui, na thread da interface

        def confirmar(nomes) -> None:
            if not nomes:
                self.log("Não há fotos no telemóvel." if todas else "Não há fotos novas no telemóvel.")
                return
            if todas:
                aviso = (f"Descarregar TODAS as {len(nomes)} foto(s) da pasta da câmara do telemóvel "
                         f"para:\n{destino}\n\nAs que já existirem aí (mesma data/hora e tamanho) "
                         "são ignoradas.")
            else:
                aviso = f"Descarregar {len(nomes)} foto(s) ainda não descarregadas para:\n{destino}"
            if apagar:
                aviso += ("\n\nATENÇÃO: cada foto descarregada será APAGADA do telemóvel depois de a "
                          "transferência ser verificada. As ignoradas ficam no telemóvel.")
            if not messagebox.askyesno("Descarregar fotos", aviso):
                return
            self.executar(
                "descarga",
                lambda: self.tel.descarregar_lista(
                    nomes, destino, self.registo, apagar, self.log, ignorar_iguais=True),
                terminou,
            )

        def terminou(res) -> None:
            feitas, ignoradas, erros = res
            msg = f"Descarregadas {feitas} foto(s)"
            if ignoradas:
                msg += f" ({ignoradas} já existiam no destino)"
            self.log(msg + ".")
            for e in erros:
                self.log(f"  falhou: {e}")

        self.executar(
            "contar fotos",
            (lambda: sorted(self.tel.listar())) if todas else (lambda: self.tel.novas(self.registo)),
            confirmar,
        )

    def escolher_destino(self) -> None:
        """Abre o seletor de pastas para escolher onde guardar as fotos."""
        atual = self.pasta_raw
        pasta = filedialog.askdirectory(
            title="Pasta de destino das fotos",
            initialdir=str(atual if atual.exists() else PASTA_APP),
        )
        if pasta:  # vazio se o utilizador cancelar
            self.var_destino.set(str(Path(pasta)))
            self.log(f"Destino das fotos: {self.var_destino.get()}")

    def abrir_camara(self) -> None:
        """Acorda o telemóvel e abre a câmara."""
        self.executar(
            "abrir câmara",
            self.tel.abrir_camara,
            lambda ok: self.log(
                "Câmara aberta." if ok else
                "A câmara não abriu: desbloqueia o telemóvel (PIN) e tenta de novo."),
        )

    def manter_ligado(self) -> None:
        """Ecrã sempre ligado e câmara aberta (reabre-se sozinha se fechar)."""
        original = self.cfg.dados.get("energia_original")  # já guardado numa utilização anterior?
        self.var_auto.set(True)

        def terminou(orig) -> None:
            # Guarda os valores originais para o botão "Repor energia normal".
            self.cfg.dados["energia_original"] = orig
            self.cfg.guardar()
            self.log("Ecrã sempre ligado com o cabo ligado (tempo limite de 30 min sem cabo). "
                     "A câmara será reaberta automaticamente se fechar.")

        self.executar("manter ligado", lambda: self.tel.manter_ligado(original), terminou)

    def repor_energia(self) -> None:
        """Volta às definições de energia que o telemóvel tinha antes."""
        original = self.cfg.dados.get("energia_original")
        self.var_auto.set(False)  # deixa de reabrir a câmara automaticamente

        def terminou(restaurou) -> None:
            self.cfg.dados.pop("energia_original", None)
            self.cfg.guardar()
            self.log("Definições de energia repostas." if restaurou else
                     "Sem valores guardados: só desliguei o 'ecrã sempre ligado'; "
                     "o tempo limite do ecrã ficou como está.")

        self.executar("repor energia", lambda: self.tel.repor_energia(original), terminou)

    # -- pré-visualização do ecrã do telemóvel

    def atualizar_preview(self) -> None:
        """Captura o ecrã do telemóvel e mostra-o à direita."""
        self.executar("pré-visualização", self.tel.captura_ecra, self._mostrar_preview)

    def _mostrar_preview(self, png) -> None:
        largura = int.from_bytes(png[16:20], "big")  # a largura está no cabeçalho do PNG
        self.escala = max(1, round(largura / LARGURA_PREVIEW))
        img = tk.PhotoImage(data=base64.b64encode(png)).subsample(self.escala, self.escala)
        self.imagem_preview = img  # guardar a referência, senão o Tk apaga a imagem
        self.canvas.delete("all")
        self.canvas.configure(width=img.width(), height=img.height())
        self.canvas.create_image(0, 0, anchor="nw", image=img)
        self._marcar_foco()

    def _clicar_preview(self, ev) -> None:
        """Um clique na pré-visualização define o ponto de foco (em pixéis reais do telemóvel)."""
        if self.imagem_preview is None:
            return
        x, y = ev.x * self.escala, ev.y * self.escala
        self.var_x.set(str(x))
        self.var_y.set(str(y))
        self.var_foco.set(True)
        self._marcar_foco()
        self.log(f"Ponto de foco: {x}, {y}")

    def _marcar_foco(self) -> None:
        """Desenha uma mira vermelha no ponto de foco escolhido."""
        self.canvas.delete("marca")
        if self.imagem_preview is None:
            return
        try:
            x, y = int(self.var_x.get()) // self.escala, int(self.var_y.get()) // self.escala
        except ValueError:
            return
        r = 12
        cor = "#ff3b30"
        self.canvas.create_oval(x - r, y - r, x + r, y + r, outline=cor, width=2, tags="marca")
        self.canvas.create_line(x - r - 4, y, x + r + 4, y, fill=cor, tags="marca")
        self.canvas.create_line(x, y - r - 4, x, y + r + 4, fill=cor, tags="marca")

    # -- leitura periódica do estado

    def _ciclo_estado(self) -> None:
        """De 5 em 5 segundos, atualiza o painel "Estado" (se não houver outra tarefa em curso)."""
        if self.a_fechar:
            return
        if not self.ocupado and not self.estado_em_curso:
            self.estado_em_curso = True
            self.executor.submit(self._tarefa_estado, self.var_auto.get())
        self.raiz.after(5000, self._ciclo_estado)

    def _tarefa_estado(self, auto: bool) -> None:
        """Lê o estado do telemóvel; com ``auto``, reabre a câmara se tiver fechado."""
        try:
            est = self.tel.estado()
            if auto and est["ligacao"] == "device" and not est["camara"]:
                self.log("Câmara fechada: a reabrir...")
                self.tel.abrir_camara()
                est = self.tel.estado()
        except ErroTelemovel as e:
            est = {"erro": str(e)}
        self._ui(lambda: self._mostrar_estado(est))

    def _mostrar_estado(self, est: dict) -> None:
        self.estado_em_curso = False
        v = self.estado_vars
        if "erro" in est:
            v["Telemóvel"].set("sem ligação ao adb")
            for k in ("Ecrã", "Câmara", "Energia"):
                v[k].set("—")
            if est["erro"] != self._ultimo_erro:  # só escreve o erro quando muda, não de 5 em 5 s
                self._ultimo_erro = est["erro"]
                self._escrever(f"{datetime.now():%H:%M:%S}  {est['erro']}\n")
            return
        self._ultimo_erro = None
        ligacao = est["ligacao"]
        if ligacao == "device":
            v["Telemóvel"].set(f"ligado ({est['serial']})")
            v["Ecrã"].set("ligado" if est["ecra"] else "desligado/bloqueado")
            v["Câmara"].set("aberta" if est["camara"] else "fechada")
            v["Energia"].set(descrever_energia(est["energia"]))
        else:
            texto = "por autorizar (aceita no ecrã do telemóvel)" if ligacao == "unauthorized" else "não detetado"
            v["Telemóvel"].set(texto)
            for k in ("Ecrã", "Câmara", "Energia"):
                v[k].set("—")

    def _fechar(self) -> None:
        """Guarda as opções e fecha a janela."""
        self.a_fechar = True
        try:
            self._guardar_opcoes()
        except OSError:
            pass  # pasta sem permissões de escrita: perde-se apenas a configuração
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.raiz.destroy()


def main() -> None:
    """Ponto de entrada: migra ficheiros antigos e abre a janela."""
    if tk is None:
        sys.exit("O Tkinter não está disponível nesta instalação do Python.")
    migrados = migrar_ficheiros_antigos()  # antes de a App ler a configuração e o registo
    raiz = tk.Tk()
    app = App(raiz)
    for m in migrados:
        app.log(f"Ficheiro migrado: {m}")
    raiz.mainloop()


if __name__ == "__main__":
    main()
