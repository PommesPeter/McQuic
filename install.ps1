Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSDefaultParameterValues['*:ErrorAction']='Stop'


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

    conda create -y -n mcquic python=3.9 cudatoolkit torchvision pytorch -c pytorch

    conda activate mcquic

    conda install -y tqdm pybind11 pip "tensorboard<3" "rich<11" "python-lmdb<2" "pyyaml<7" "marshmallow<4" "click<9" "vlutils" "msgpack-python<2" packaging -c xiaosu-zhu -c conda-forge

    if ($env:CONDA_DEFAULT_ENV -ine "mcquic")
    {
        Write-Output "Can't activate conda env mcquic, exit."
        exit 1
    }

    $env:ADD_ENTRY = "SET"

    pip install -e .

    python ci/post_build/win_install_post_link.py $env:CONDA_PREFIX

    Write-Output "Installation done!"

    Write-Output "If you want to train models, please install NVIDIA/Apex manually."
    Write-Output "If you want to use streamlit service, please install streamlit via pip."
}
else
{
    Write-Output "conda could not be found, please ensure you've installed conda and place it in PATH."
}
