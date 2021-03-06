# Launch a Program via Voice

Listens for a wake word, then launches a program based on a voice command.

## Setup

Once you have `voice2json` installed and a profile downloaded, copy `sentences.ini` and the `slots` directory into your profile directory (probably `$HOME/.config/voice2json`). Depending on whether you're using a Pocketsphinx or Kaldi-based profile, copy `custom_words.pocketsphinx.txt` or `custom_words.kaldi.txt` (respectively) to `custom_words.txt` in your profile. Make sure to backup your profile first if you've done any customization!

Next, run the `listen_and_launch.sh` script:

```bash
$ ./listen_and_launch.sh
```

Try saying the wake word into your microphone (default is "porcupine"). The program will beep when it's been recognized. Now, say a voice command like "run firefox". After a period of silence, another beep will play. If all goes well, the `listen_and_launch` script will attempt to run `/usr/bin/firefox`.
