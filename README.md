# Sound Activated Audio Recorder

Auto Record is a Python script that records audio in WAV files.
Instead of recording continuously, Auto Record creates multiple
recordings, starting when noise is detected and stopping when there
is a period of silence.

The WAV files can be accessed via a simple web service implemented
in web_server.py

Run `auto_record.py` when there are only ambient sounds. It first builds an ambient baseline for 10 seconds and then listens for noises and automatically records them as WAV files into the sub-directory `data`.

# How this project works

Auto-recording is triggered by **detecting a rising RMS sound level** in the audio input stream. Here is how and when it works:

## Prerequisite: Recording Must Be Enabled
Before any trigger can fire, recording must be enabled. This is controlled by the existence of a flag file at `data/record`:
- If the file exists, the monitor is **armed** and listening for sound.
- If it does not exist, audio is read but discarded.
- You can toggle this via the web UI (`main.html`) or by starting `auto_record.py` with the `disabled` argument.

## How the Trigger Works (`auto_record.py`)
The script runs in a continuous loop reading audio blocks of 2048 frames at 44.1 kHz. Each block is about 46 ms of 16-bit PCM audio.

### 1. Baseline Mode
- At startup, normal microphone mode and command-input mode both spend 10 seconds measuring ambient RMS volume.
- The baseline stores the ambient mean and standard deviation.
- The resulting baseline threshold is used as a fixed gate, so small local changes inside the ambient noise floor do not trigger recording.

### 2. Listening Mode
- The script maintains a rolling RMS history while listening.
- For each new block, it compares recent smoothed RMS against an older smoothed RMS window.
- This detects a **rising sound trend**, not just a fixed loudness level.

### 3. Start Trigger
Recording starts only when both conditions are true:
- The recent smoothed RMS rises fast enough above an older local RMS window.
- The recent smoothed RMS is also above the startup ambient baseline threshold.

This makes the recorder less sensitive to steady fan noise, background hum, or small ambient drift.

### 4. Noise Threshold
- **Startup baseline:** Each run measures 10 seconds of ambient sound before triggering can start.
- **Default continuation threshold:** `0.1` normalized RMS is still used while recording to decide when enough silence has passed to stop.
- **Calibrated continuation threshold:** If `data/calibrated` exists, that value is loaded and used instead of the default continuation threshold.

## When Recording Stops
Once recording starts, it stops only when:
- **2 seconds of continuous silence** pass. The file keeps up to 2 seconds of trailing silence for natural padding.
- **Recording is disabled** mid-capture (via web UI or deleting the `data/record` file). The current file is finalized immediately.

## Output Filter
Recordings shorter than **1.5 seconds** are automatically deleted; only longer ones are kept as `.wav` + `.json` pairs in the `data/` directory.

## Command Input Mode
For testing and batch processing, `auto_record.py` can read raw 16-bit PCM audio from another command instead of the microphone:

```
python auto_record.py command ffmpeg.exe -i samples/s3.wav -loglevel quiet -f s16le -acodec pcm_s16le -ac 1 -ar 44100 -
```

Command mode expects mono signed 16-bit little-endian PCM at 44.1 kHz on stdout. It uses the same 10-second baseline, trigger, stop, and output logic as microphone mode. JSON metadata for command-mode recordings includes:
- `command_timestamp`: wall-clock time when the command was started.
- `source_start_seconds`: start time of the snippet in the source audio stream.
- `source_end_seconds`: end time of the snippet in the source audio stream.

---

### Summary
| Condition | Action |
|---|---|
| `data/record` file missing | No monitoring; nothing is recorded |
| 10-second startup baseline complete | Begin listening for rising RMS events |
| RMS slope trigger exceeds local and baseline thresholds | **Start recording** |
| 2 seconds of silence | Stop recording and save |
| Recording disabled mid-capture | Stop and save immediately |
| Final file < 1.5 seconds | Discard |

## Scripts

This project has the following scripts:

- auto_record.py: The noise triggered audio recorder
- web_server.py: Web server to access audio recordings
- wav2csv.py: Convert WAVE files to CSV and plot audio data
- play.py: Sample script to play audio files
- record.py: Simple example script to record WAVE files


## Dependencies

The audio recorder requires pyaudio which requires the portaudio package.
The web server requires fastapi

First, ensure you have pip and venv:
```
sudo apt install pip
sudo apt install python3-venv
```

Install the portaudio19 dev package:
```
sudo apt install portaudio19-dev
```

Create an isolated venv and install the required package

```
python3 -m venv venv 
source ./venv/bin/activate
pip install -r requirements.txt
```

## Running the Audio Recorder

The recording script can be run directly from the command line:

python auto_record.py [ debug ] [ calibrate | disabled | command <input command> ]
- debug: print debug information
- calibrate: run an interactive audio calibration and exit
- disabled: start the recorder with recording disabled
- command: read raw PCM audio from a command instead of the microphone

```
source ./venv/bin/activate
python auto_record.py
```

## Running the Web Server

The web server provides access to the recordings and the ability to enable
and disable recording.


```
source ./venv/bin/activate
python web_server.py
```

Connect to localhost:3000 to see the list of recordings.

## Calibration

Calibrate the continuation threshold by running an interactive calibration process.
The process measures 10 seconds of quiet ambient audio and writes a threshold to
`data/calibrated`. Normal recording also builds a 10-second startup baseline on
each run, so calibration is optional for start detection.

```
source ./venv/bin/activate
python auto_record.py calibrate
```

## State Files

The auto_record.py script creates a data directory under the working directory.
In this directory, it creates:
- .wav files - recordings
- .json files - information for each recording in json format
- record - if present, recording is enabled
- calibrated - created by the calibraiton process.

## Changes Since Commit 6637faf

The current behavior includes the functional changes introduced from commit `6637faf` onward:
- Startup recording now establishes a 10-second ambient baseline before start detection is allowed.
- Start detection uses a sliding-window RMS trend/slope check plus the ambient baseline threshold.
- The minimum retained recording length is 1.5 seconds.
- Audio reads use larger 2048-frame blocks at 44.1 kHz and tolerate PyAudio input overflows without crashing.
- Output filenames include microseconds to avoid collisions when multiple snippets are created in the same second.
- Command input mode can simulate a microphone from ffmpeg or another raw PCM producer.
- Command-mode JSON includes command start time and source audio offsets.


## Web Service

Start the web server providing access to the audio files.
Note that there is no access control for this web server.

```
source ./venv/bin/activate
python web_server.py
```

## Automatic Startup

In addition to being run directly from the command line, the 
scripts can be run with systemd for automatic start on boot up.
There are issues with the default sound input device being different
when run from systemd. The auto_record_log.txt file can be helpful
in troubleshooting issues.

Note that on the RaspberryPi, the sound card needs to be configured
in /etc/asound.conf.  Run cat /proc/asound/cards to see the existing
sound options.

For more info, see: 
[[SOLVED] Help: ffmpeg and alsa - work as script not as systemd service problem](https://forums.raspberrypi.com/viewtopic.php?t=278665)

### Configure Autostart

Change the User= and WorkingDirectory= lines
in the auto_record.service and auto_record_server.service files.
Install the files:

```
sudo cp auto_record.service /lib/systemd/system
sudo cp auto_record_server.service /lib/systemd/system
sudo systemctl daemon-reload
sudo systemctl enable auto_record
sudo systemctl enable auto_record_server
```

Start the services:

```
sudo systemctl start auto_record
sudo systemctl start auto_record_server
sudo systemctl status auto_record
sudo systemctl status auto_record_server
```


### Shutdown and Removing Files

To remove the installed systemd configuration:

```
sudo systemctl stop auto_record
sudo systemctl stop auto_record_server
sudo systemctl disable auto_record
sudo systemctl disable auto_record_server
sudo rm /lib/systemd/system/auto_record.service 
sudo rm /lib/systemd/system/auto_record_server.service 
```

## Further Work

There are a few areas where the audio recorder could likey use improvement.

### Select an Audio Device

The auto_record.py script opens the default sound input device. When run 
from a desktop environment, this is usually configured correctly. However
when starting from systemd for an automatic boot, the script may pick the
wrong device. A few to pass in a device name may be useful.

### Criteria to Start Recordings

The criteria for starting a recording is very simplistic. There may be better
ways to detect when to start the recording.

### Convert WAVE Files to MP3

WAVE files are large and require a great deal of storage. Any application
that collects a significant number of hours of recording would benefit
from a feature that converts WAVE files to MP3 files.


### Cotrol Calibration via Web Browser

Once the auto recording script is configured to start on boot, it is 
inconvenient to run a calibration. You need to stop the service, run the
calibraiton from the command line, and then restart the service. It would
be useful to invoke calibration via the web server interface.

It also isn't clear that the environments running at boot time and
from the command line are the same and would produce the same
calibration values.


## Graphing Sound Files

It is facinating to look at sound files graphically to understand the nature
of the signals. The wav2csv.py module reads a wav file and writes a CSV file
containing the audio samples. It has some additional routines to create
simple plots of the audio data.

### Required Packages

In order to analyze and plot sound file data, you need two additional modules not included
in the requriements.txt:

```
pip install numpy
pip install matplotlib
```
### View Audio 

To view the data as well as generate a CSV file, run the script with the -i argument to python:
```
python -i wav2csv.py <WAVE file>
```

After the CSV file is written, the sound data is in the numpy ndarray array:

```
python -i wav2csv.py data/2025-01-20_16:14:25.wav
Write file: data/2025-01-20_16:14:25.wav.csv
Done
>>> array
array([[ -15,  -17],
       [  -6,   -8],
       [  18,   17],
       ...,
       [-122, -122],
       [-151, -150],
       [-136, -136]], shape=(27000, 2))
```

### Plot the Audio Data

Plot all audio data

```
plot_array(array)
```

![Sound Wave](./images/sound.png)

Plot only the first channel of the array:

```
plot_array(array[:,0])
```

Noom in, plotting samples 9000 to 10000

```
plot_array(array[9000:10000,0])
```

![Zoom in Sound Wave](./images/sound-zoom.png)

### Plot Volume

Plot the volume of each block of audio:

```
python
>>> import wav2csv as w
>>> blocks = w.read_data_blocks("data/2025-01-20_16:14:25.wav")
>>> volume = w.get_volume_array(blocks)
>>> w.plot_stairs(volume)
```


![Zoom in Sound Wave](./images/volume.png)
