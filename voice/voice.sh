#!/bin/bash

# Load or prompt for password if not already saved
if [ ! -f ~/.tempaccess ]; then
    read -sp "Enter your password: " PASSWORD
    echo
    echo "$PASSWORD" > ~/.tempaccess
    chmod 600 ~/.tempaccess
    
# Ensure required packages are installed
sudo apt-get update && \
sudo apt-get install -y libpython3-dev python3-venv

else
    PASSWORD=$(cat ~/.tempaccess)
fi

# Create a script to cache sudo privileges
CACHE_SCRIPT="/tmp/cache_sudo.sh"
echo -e "#!/bin/bash\necho \"$PASSWORD\" | sudo -S true" > $CACHE_SCRIPT
chmod +x $CACHE_SCRIPT

# Run the cache script to ensure sudo privileges are cached
bash $CACHE_SCRIPT

# Create the voice directory and download files if necessary
mkdir -p "/home/$(whoami)/voice"
[ -f /home/$(whoami)/voice/glados_piper_medium.onnx ] || \
curl -L https://raw.githubusercontent.com/robit-man/EGG/main/voice/glados_piper_medium.onnx -o /home/$(whoami)/voice/glados_piper_medium.onnx
[ -f /home/$(whoami)/voice/glados_piper_medium.onnx.json ] || \
curl -L https://raw.githubusercontent.com/robit-man/EGG/main/voice/glados_piper_medium.onnx.json -o /home/$(whoami)/voice/glados_piper_medium.onnx.json
[ -f /home/$(whoami)/voice/inference.py ] || \
curl -L https://raw.githubusercontent.com/robit-man/EGG/main/voice/inference.py -o /home/$(whoami)/voice/inference.py

# Check if required scripts are present
if [ -f "/home/$(whoami)/voice/audio_stream.py" ] && [ -f "/home/$(whoami)/voice/whisper_server.py" ]; then
    # Run commands in GNOME terminals with sudo privileges cached
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && cd /home/$(whoami)/voice && python3 audio_stream.py; exec bash"
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && cd /home/$(whoami)/voice && python3 model_to_tts.py --stream --history; exec bash"
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && jetson-containers run -v \"$(pwd)/voice:/voice\" \"\$(autotag piper-tts)\" bash -c 'cd /voice && python3 inference.py'; exec bash"
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && jetson-containers run -v /home/$(whoami)/voice:/voice \$(autotag whisper) bash -c 'cd /voice && python3 whisper_server.py'; exec bash"
else
    # Clone the repository and copy necessary files
    git clone --depth=1 --filter=blob:none --sparse https://github.com/robit-man/EGG.git /tmp/EGG
    cd /tmp/EGG && git sparse-checkout set voice/whisper
    cp -r voice/whisper/* "/home/$(whoami)/voice/"
    cd "/home/$(whoami)/voice"

    # Run commands in GNOME terminals with sudo privileges cached
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && python3 audio_stream.py; exec bash"
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && python3 model_to_tts.py --stream --history; exec bash"
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && jetson-containers run -v \"$(pwd)/voice:/voice\" \"\$(autotag piper-tts)\" bash -c 'cd /voice && python3 inference.py'; exec bash"
    gnome-terminal -- bash -c "bash $CACHE_SCRIPT && jetson-containers run -v /home/$(whoami)/voice:/voice \$(autotag whisper) bash -c 'cd /voice && python3 whisper_server.py'; exec bash"

    # Clean up
    rm -rf /tmp/EGG
fi