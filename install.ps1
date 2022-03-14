function Check-Command($cmdname)
{
    return [bool](Get-Command -Name $cmdname -ErrorAction SilentlyContinue)
}

$ErrorActionPreference = "Stop"

$checked = Read-Host "Please ensure you are running Anaconda Powershell Prompt [y/n]"

if ($checked -ine "y")
{
    exit
}

if (Check-Command -cmdname 'conda')
{
    Write-Output "Start installation"

    conda create -y -n mcquic cudatoolkit tqdm pybind11 pip "tensorboard<3" "rich<11" "python-lmdb<2" "pyyaml<7" "marshmallow<4" "click<9" "vlutils" "msgpack-python<2" -c xiaosu-zhu -c conda-forge -c pytorch

    conda activate mcquic

    if ($env:CONDA_DEFAULT_ENV -ine "mcquic")
    {
        Write-Output "Can't activate conda env mcquic, exit."
        exit 1
    }

    Copy-Item "setup.cfg" -Destination "setup.cfg.bak"

    python ci/pre_build/cfg_entry_points.py setup.cfg

    pip install -e .

    $env:PREFIX = $env:CONDA_PREFIX

    cmd.exe /c "conda/post-link.bat"

    Remove-Item "setup.cfg"

    Copy-Item "setup.cfg.bak" -Destination "setup.cfg"

    Write-Output "Installation done!"

}
else
{
    Write-Output "conda could not be found, please ensure you've installed conda and place it in PATH."
}
