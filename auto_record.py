"""
Monitor the default audio input and record active sounds. 
Trigger recording on 0.25 sec of sound, stop recording after 5s of silence.
Silence is a sustained noise level that does not meet the defined threshold.

Generate seperate WAVE files for each active recording.
Timestamps are encoded into the file name.
Record 2 channel 16bit 8khz sampling.

Support:
    - interactive calibration
    - start up with recording disabled
    - run with debug statements

2 states:
- listening: wait to detect sufficient noise to start recording
buffer .5 sec of silence to include in a recording
When a burst noise is detected, transition to recording

- recording: record audio, detect 10 seconds of silence to stop recording
write a maximum of 1s of silence at the end of the recording
(this means buffer up to 9 sec of silence while detecting end of recording)
"""

import wave
import datetime
import math
import struct
import os
import json
import sys
import logging
import logging.config
import collections
import pyaudio


DATA_DIR = "data"               # Directory for output files
RECORD_ENABLED_FILE = "record"  # File existance enables / disables recording
CALIBRATION_FILE = "calibrated" # Noise threshold for calibration
RATE = 44100                     # 4.41 KZ sampling - 44100 samples per second
BLOCK_SIZE = 2048                # Default read size - 16 blocks per second
FORMAT = pyaudio.paInt16        # LE 16 bit, a common format
CHANNELS = 2                    # 2 channel sterio is a comon format

# Number of blocks of silence to trigger recording stop - 10 seconds
SILENCE_TRIGGER_DURATION = 10 * RATE/BLOCK_SIZE  

# Number of blocks of silence to write before pausing - 2 seconds
SILENCE_WRITE_DURATION = 2 * RATE/BLOCK_SIZE        

# Number of blocks of silence to keep in listening mode - 1
SILENCE_LISTEN_DURATION = RATE/BLOCK_SIZE    

# Default threshold for noise/silence normalized to 1.0
DEFAULT_NOISE_THRESHOLD=0.1    
START_TRIGGER_WINDOW = 6
START_TRIGGER_REQUIRED_NOISY_BLOCKS = 4
CALIBRATION_STD_DEV_MULTIPLIER = 6
CALIBRATION_MIN_BASELINE_MULTIPLIER = 3
CALIBRATION_MIN_ABSOLUTE_THRESHOLD = 0.003

# Logging object
logger = logging.getLogger('auto_record')

def calc_rms(samples: list[float]) -> float:
    """
    Calculate the amplitude of a WAVE data block
    Normalized to 1.0

    Square root of the mean over time of the square of the amplitude.
    """
    sum_squares = 0.0
    # iterate over the block.
    for sample in samples:
       sum_squares += sample*sample

    return math.sqrt( sum_squares / len(samples) )


class AudioDataBlock:
    """
    Represents a block of audio data. Audio data is a set of frames, where
    each frame is a set of samples from a set of channels. 

    The data is decoded into normalized samples and the volume of the data block
    is computed for later usage.

    The data is assumed to be LE signed shorts. We assume 2 samples per frame
    and read in BLOCK_SIZE frames. With BLOCK_SIZE as 1000 frames, we read 4000 bytes that encode
    2000 samples.

    Enhancement: add awareness of encoding and number of channels.
    """
    def __init__(self, data: bytes):
        self.data : bytes = data
        self.samples : list[float] = self.unpack_data()
        self.volume : float = calc_rms(self.samples)

    def is_noisy(self, threshold: float) -> bool:
        """
        Return true if volume of block meets noise threadhold.
        """
        return self.volume >= threshold

    def unpack_data(self) -> list[float]:
        """
        Unpack data encoded as 2 bytes per value. 
        Result value is a signed short int. Normalized to 1.0
        """
        # Convert the string of bytes into a list of 16 bit (short) samples
        # Normalize to 1.0
        count = len(self.data) // 2
        format = "<%dh"%(count)
        shorts = struct.unpack( format, self.data )

        # sample is a signed short in +/- 32768. 
        # normalize it to 1.0
        SHORT_NORMALIZE = (1.0/32768.0)
        return [ float(v) * SHORT_NORMALIZE for v in shorts]

    
class AutoRecordSession:
    """
    An active audio monitoring session.
    """
    def __init__(self):
        self.audio: pyaudio.PyAudio = pyaudio.PyAudio()          # Provides the audio input stream
        self.out_file = None                    # Open file for writing, if any
        self.open_time = None                   # datetime.datetime of recording start (within a few seconds)
        self.out_file_name = None               # Open file for writing, if any
        self.in_stream = None                   # PyAudio input stream
        self.is_recording = False               # Listening or Recording
        self.silence_count = 0                  # Count number of seqential silent blocks read
        self.noise_threashold = DEFAULT_NOISE_THRESHOLD

        # Buffered data
        self.data_queue: collections.deque[AudioDataBlock] = collections.deque()

    def start_session(self, enabled: bool):
        # Ensure data directory exists
        try:
            os.mkdir(DATA_DIR)
        except FileExistsError:
            pass

        if os.path.isfile(self.calibration_file()):
            file = open(self.calibration_file(), 'r')
            value = file.read()
            file.close()
            value = float(value)
            if value > 0:
                logger.info("Setting threshold to %f" % value)
                self.noise_threashold = value

        # Initialize enabled status
        path = os.path.join(DATA_DIR, RECORD_ENABLED_FILE)
        if enabled:
            f = open(path,'w')
            f.close()
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        logger.info("Default device:")
        info = self.audio.get_default_input_device_info()
        logger.info(
            "index: %d, name: %s, max input channels: %d, default sample rate: %d",
            info['index'], 
            info['name'], 
            info['maxInputChannels'],
            info['defaultSampleRate'])

        logger.info("All devices:")
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            logger.info(
                "index: %d, name: %s, max input channels: %d, default sample rate: %d",
                info['index'], 
                info['name'], 
                info['maxInputChannels'],
                info['defaultSampleRate'])

        logger.info("Opening audio stream")
        self.in_stream = self.audio.open(format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=BLOCK_SIZE)
        logger.info("Audio stream open")

    def read_audio_data(self) -> bytes | None:
        try:
            return self.in_stream.read(BLOCK_SIZE, exception_on_overflow=False)
        except OSError as err:
            logger.warning("Audio input overflow; skipping one block: %s", err)
            return None

    def cleanup_session(self):
        """
        Stop and cleanup
        """
        if self.in_stream is not None:
            self.in_stream.stop_stream()
            self.in_stream.close()
            self.in_stream = None
        self.audio.terminate()

    def run(self, enabled: bool):
        """
        Main loop for script. 
        Runs listening and recording functions.
        """
        self.start_session(enabled)
        try:
            while self.in_stream is not None:
                # Read a block of audio
                data = self.read_audio_data()
                if data is None:
                    continue

                # if we are not enabled for recording, ensure any
                # in process recording is completed and the buffer is empty
                if not self.check_recording_enabled():
                    continue

                # Buffer data if we are enabled for recording.
                block = AudioDataBlock(data)
                self.data_queue.append(block)

                # Run listening and recording logic
                if not self.is_recording:
                    self.run_listen_logic()
                elif self.is_recording:
                    self.run_record_logic()

        except KeyboardInterrupt:
            # Interupted, complete current recording
            if self.is_recording:
                self.stop_recording()
        self.cleanup_session()

    def start_recording(self) -> None:
        """
        Start the recording session, change to recording mode
        """
        self.is_recording = True
        self.silence_count = 0
        self.ensure_open_file()
        self.report_status("start recording")

    def stop_recording(self) -> None:
        """
        Complete the recording, change to listen mode
        """
        self.is_recording = False
        self.ensure_close_file()
        # Ensure no extra data is buffered
        while len(self.data_queue) > SILENCE_LISTEN_DURATION:
            self.data_queue.popleft()

    def run_listen_logic(self) -> None:
        """
        Detect start of recording.

        Uses count of silent blocks and the noise_start_candidate block index 
        to potentially set the mode to RECORD.

        Assumed: mode == LISTEN
        """
        # Check for 2 of 3 noisy frames
        if len(self.data_queue) >= START_TRIGGER_WINDOW:
            # Check if noise threashold exceeded
            count = 0
            for index in range(-1, -START_TRIGGER_WINDOW - 1, -1):
                if self.data_queue[index].is_noisy(self.noise_threashold):
                    count += 1
            if count >= START_TRIGGER_REQUIRED_NOISY_BLOCKS:
                # Trigger recording
                self.start_recording()

        # Discard frames that will never be recorded
        if not self.is_recording:
            while len(self.data_queue) > SILENCE_LISTEN_DURATION:
                self.data_queue.popleft()

    def run_record_logic(self) -> None:
        """
        Detect write pause and end of recording.
        
        Use the count of silent blocks to set / clear silence pause
        and potentially change the mode to LISTEN

        Assumed: mode == RECORD
        """
        while len(self.data_queue) > 0:
            block = self.data_queue.popleft()

            # Count consective silent frames
            if block.is_noisy(self.noise_threashold):
                logger.debug("found noise: %f" % block.volume)
                self.silence_count = 0
            else:
                self.silence_count += 1

            # Write buffer if we have not seen the limit of silent frames.
            if self.silence_count < SILENCE_WRITE_DURATION:
                # Write buffer
                if self.out_file is not None:
                    self.out_file.writeframes(block.data)

            # Stop recoording if we have exceeded the wait duration for more noise
            if self.silence_count > SILENCE_TRIGGER_DURATION:
                # Long silence, stop recording
                self.stop_recording()
                self.report_status("stopped recording due to silence")

    def check_recording_enabled(self) -> bool:
        """
        Check if recording is enabled and handle
        case where we need to complete an inprocess recording.
        """
        path = os.path.join(DATA_DIR, RECORD_ENABLED_FILE)
        enabled = os.path.isfile(path)
        if not enabled:
            if self.is_recording:
                # Finish inprocess recording
                self.stop_recording()
                self.report_status("stopped recording on disable")
        return enabled

    def calibration_file(self) -> str:
        """
        Return the path to the calibration file that holds the threadhold value.
        (If it exists)
        """
        return os.path.join(DATA_DIR, CALIBRATION_FILE)

    def ensure_open_file(self) -> None:
        """
        Open a WAV file for output if it is not already open
        """
        if self.out_file is not None:
            return
        
        now = datetime.datetime.now()
        self.open_time = now
        self.out_file_name = "%04d-%02d-%02d_%02d_%02d_%02d" % (
            now.year, now.month, now.day, now.hour, now.minute, now.second
        ) 

        path = os.path.join(DATA_DIR, "%s.tmp" % self.out_file_name)
        self.out_file = wave.open(path, 'wb')
        self.out_file.setnchannels(CHANNELS)
        self.out_file.setsampwidth(self.audio.get_sample_size(FORMAT))
        self.out_file.setframerate(RATE)

    def ensure_close_file(self) -> None:
        """
        Close the output WAV file and write information in a json file.
        If the output file is not open, does nothing.
        """
        if self.out_file is None:
            return

        # Close file
        self.out_file.close()

        # Rename file and create a meta file to document it
        tmp_name = "%s.tmp" % self.out_file_name
        json_name = "%s.json" % self.out_file_name
        wav_name = "%s.wav" % self.out_file_name

        logger.info(f"Closing file {wav_name}")

        path_tmp = os.path.join(DATA_DIR, tmp_name)
        path_wav = os.path.join(DATA_DIR, wav_name)
        path_json = os.path.join(DATA_DIR, json_name)

        recording_length = int(self.out_file.getnframes() / RATE)

        info = {"sound_file": wav_name,
                "basename": self.out_file_name,
                "json_file": json_name,
                "timestamp": self.open_time.isoformat(),
                "length": recording_length }

        # Capture files that are 2 seconds or longer
        if recording_length >= 2:
            os.rename(path_tmp, path_wav)
            with open(path_json, 'w') as file:
                file.write(json.dumps(info))
        else:
            os.remove(path_tmp)

        self.out_file = None
        self.out_file_name = None
        self.open_time = None

    def calibrate(self):
        """
        Run a silence-based calibration to generate a noise threshold level.
        Measures ambient silence and sets the threshold well above it so that
        any real sound triggers recording automatically.
        Write out result in the data/calibrated file.
        """
        self.start_session(False)

        wait_time = 10
        print("\n\nAudio Level Calibration")
        print(f"Please remain quiet for {wait_time} seconds to measure the silence baseline...")

        # Collect wait_time seconds of silence data
        volumes = []
        num_blocks = int(wait_time * RATE / BLOCK_SIZE)

        try:
            for _ in range(num_blocks):
                data = self.read_audio_data()
                if data is None:
                    continue
                block = AudioDataBlock(data)
                volumes.append(block.volume)

            if len(volumes) == 0:
                print("Calibration failed: no audio blocks were captured.")
                return

            # Compute mean and standard deviation of silence
            mean = sum(volumes) / len(volumes)
            variance = sum((v - mean) ** 2 for v in volumes) / len(volumes)
            std_dev = math.sqrt(variance)

            # Threshold = mean + 3 standard deviations.
            # This captures >99% of normal silence fluctuation while
            # reliably triggering on any genuine sound.
            threshold = mean + CALIBRATION_STD_DEV_MULTIPLIER * std_dev

            # Prevent impossibly low thresholds in near-digital-silence conditions
            threshold = max(
                threshold,
                mean * CALIBRATION_MIN_BASELINE_MULTIPLIER,
                CALIBRATION_MIN_ABSOLUTE_THRESHOLD,
            )

            print("Baseline volume: %f" % mean)
            print("Standard deviation: %f" % std_dev)
            print("Calibration threshold: %f" % threshold)

            with open(self.calibration_file(), 'w') as f:
                f.write(str(threshold))
            print("Calibration value saved: %f" % threshold)

        except KeyboardInterrupt:
            pass
        self.cleanup_session()

    def report_status(self, msg: str) -> None:
        """
        Log a messaage and report value of state variables
        """
        now = datetime.datetime.now()
        mode = "Listen" if not self.is_recording else "Record"
        message = "%s. mode=%s, silence_count=%d, buffered=%d" % (
            msg, mode, self.silence_count, len(self.data_queue)
        )
        logger.info(message)


#
# Pythong logging configuration specifying a log file and an optional stream output
#
LOGGING_CONFIG = {
    'version': 1,
    'formatters': {
        'standard': {
            'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        }
    },
    'handlers': {
        'default': {
            'formatter': 'standard',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': 'auto_record_log.txt'
        },
        'stream': {
            'formatter': 'standard',
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stdout',
        }
    },
    'loggers': {
        'auto_record': {
           'handlers': [ 'default' ],
           'level' : 'INFO',
        }
    }
}

if __name__ == "__main__":
    # Handle debug argument
    if 'debug' in sys.argv:
        # Log to stream in debug mode
        LOGGING_CONFIG['loggers']['auto_record']['handlers'].append('stream')
        logging.config.dictConfig(LOGGING_CONFIG)
        logger.setLevel(logging.DEBUG)
        sys.argv.remove('debug')
    else:
        logging.config.dictConfig(LOGGING_CONFIG)

    logger.info("Starting session...")
    session = AutoRecordSession()
    enabled = True
    if len(sys.argv) > 1:
        if sys.argv[1].lower() == "calibrate":
            session.calibrate()
            sys.exit(0)
        elif sys.argv[1].lower() == "disabled":
            enabled = False
        else:
            print("usage: [debug] [calibrate | disabled]")
            sys.exit(-1)

    # Run the recording
    session.run(enabled)
