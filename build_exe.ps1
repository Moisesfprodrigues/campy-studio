# Gera dist\CamPyStudio.exe (executável único, sem consola).
# Uso (PowerShell, na pasta do projeto):  .\build_exe.ps1
python -m pip install pyinstaller Pillow
python -m PyInstaller --noconfirm --onefile --windowed `
    --name CamPyStudio `
    --icon assets\logo.ico `
    --add-data "assets;assets" `
    campy_studio.py
Write-Host "`nPronto: dist\CamPyStudio.exe (o adb continua a ser necessário no PATH)."
