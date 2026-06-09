import queue
import wave

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtMultimedia import QAudioFormat, QAudioSource
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class AudioReader(QObject):
    """Runs in Audio Thread. Permanently captures data for FFT and selectively pipes data to File queue."""

    finished = Signal()

    def __init__(self, file_queue: queue.Queue, fft_queue: queue.Queue):
        super().__init__()
        self.file_queue = file_queue
        self.fft_queue = fft_queue
        self.audio_source = None
        self.io_device = None
        self.is_recording = False  # Track file dump state

    @Slot()
    def start_hardware_capture(self):
        # Configure Audio Format: 44.1kHz, 16-bit Mono PCM
        audio_format = QAudioFormat()
        audio_format.setSampleRate(44100)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        self.audio_source = QAudioSource(audio_format, self)
        self.io_device = self.audio_source.start()

        if self.io_device:
            self.io_device.readyRead.connect(self.handle_ready_read)
        else:
            print("Failed to open audio device hardware.")

    @Slot()
    def handle_ready_read(self):
        if not self.io_device:
            return

        raw_bytes = self.io_device.readAll().data()
        if raw_bytes:
            # Always pass data to the FFT processor pipeline
            self.fft_queue.put(raw_bytes)

            # Conditionally pass data to the file recorder pipeline
            if self.is_recording:
                self.file_queue.put(raw_bytes)

    @Slot(bool)
    def set_recording_state(self, enabled: bool):
        self.is_recording = enabled

    @Slot()
    def stop_hardware_capture(self):
        if self.audio_source:
            self.audio_source.stop()
        self.finished.emit()


class AudioFileWriter(QObject):
    """Runs in File Thread. Dynamically spins up to write raw PCM blocks to a WAV file."""

    def __init__(self, data_queue: queue.Queue, filename="output.wav"):
        super().__init__()
        self.queue = data_queue
        self.filename = filename
        self.running = False
        self.wav_file = None

    @Slot()
    def start_writing(self):
        self.running = True

        # Flush out any stale, unrecorded data chunks before starting clean
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break

        self.wav_file = wave.open(self.filename, "wb")
        self.wav_file.setnchannels(1)
        self.wav_file.setsampwidth(2)
        self.wav_file.setframerate(44100)

        while self.running or not self.queue.empty():
            try:
                raw_data = self.queue.get(timeout=0.1)
                self.wav_file.writeframes(raw_data)
                self.queue.task_done()
            except queue.Empty:
                continue

        if self.wav_file:
            self.wav_file.close()

    @Slot()
    def stop_writing(self):
        self.running = False


class FFTProcessor(QObject):
    """Runs in FFT Thread. Permanently loops computing level + spectrum maps."""

    analysis_ready = Signal(dict)

    def __init__(self, data_queue: queue.Queue, sample_rate=44100):
        super().__init__()
        self.queue = data_queue
        self.sample_rate = sample_rate
        self.running = False

    @Slot()
    def start_processing(self):
        self.running = True
        while self.running:
            try:
                raw_data = self.queue.get(timeout=0.1)
                audio_data = np.frombuffer(raw_data, dtype=np.int16)

                if len(audio_data) == 0:
                    self.queue.task_done()
                    continue

                # 1. Level Detection (RMS)
                rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
                normalized_level = min(int((rms / 32767.0) * 100) * 5, 100)

                # 2. Windowed Power Spectral Density
                window = np.hanning(len(audio_data))
                windowed_audio = audio_data * window
                fft_complex = np.fft.rfft(windowed_audio)
                power_spectrum = np.abs(fft_complex) ** 2
                psd_db = 10 * np.log10(power_spectrum + 1e-10)
                freq_axis = np.fft.rfftfreq(len(audio_data), d=1.0 / self.sample_rate)

                payload = {
                    "frequencies": freq_axis.tolist(),
                    "power_spectrum": psd_db.tolist(),
                    "level": normalized_level,
                }
                self.analysis_ready.emit(payload)

                self.queue.task_done()
            except queue.Empty:
                continue

    @Slot()
    def stop_processing(self):
        self.running = False


class MainWindow(QMainWindow):
    """Runs in Main UI Thread. Starts pipeline on launch; button records to file on demand."""

    sig_init_capture = Signal()
    sig_toggle_record_state = Signal(bool)

    def __init__(self):
        super().__init__()
        self.recording_active = False

        self.init_ui()
        self.init_threads()
        self.start_always_on_pipeline()  # Launch pipeline immediately at startup

    def init_ui(self):
        self.setWindowTitle("Startup Analyzer with Toggle File Record")
        self.resize(700, 550)
        layout = QVBoxLayout()

        # Single toggle control button
        self.btn_record = QPushButton("Start Recording to File")
        self.btn_record.setStyleSheet(
            "font-weight: bold; background-color: #2196F3; color: white; padding: 6px;"
        )

        # Level Indicators
        self.lbl_level = QLabel("Real-Time Audio Level:")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #bbb; background: #eee; border-radius: 4px; height: 15px; }
            QProgressBar::chunk { 
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4CAF50, stop:0.8 #FFEB3B, stop:1 #F44336); 
                border-radius: 3px; 
            }
        """)

        # Plot Window
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("k")
        self.plot_widget.setLabel("left", "Power", units="dB")
        self.plot_widget.setLabel("bottom", "Frequency", units="Hz")
        self.plot_widget.setXRange(0, 20000)
        self.plot_widget.setYRange(0, 120)
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.spectrum_curve = self.plot_widget.plot(pen=pg.mkPen("#00FFCC", width=1.5))

        layout.addWidget(self.btn_record)
        layout.addWidget(self.lbl_level)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.plot_widget)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.btn_record.clicked.connect(self.toggle_recording_action)

    def init_threads(self):
        self.file_queue = queue.Queue()
        self.fft_queue = queue.Queue()

        # Dedicated Thread Contexts
        self.audio_thread = QThread()
        self.fft_thread = QThread()
        self.file_thread = QThread()

        # Workers
        self.reader = AudioReader(self.file_queue, self.fft_queue)
        self.fft_processor = FFTProcessor(self.fft_queue)
        self.file_writer = AudioFileWriter(self.file_queue)

        # Move to Respective Threads
        self.reader.moveToThread(self.audio_thread)
        self.fft_processor.moveToThread(self.fft_thread)
        self.file_writer.moveToThread(self.file_thread)

        # Setup Thread Routines
        self.sig_init_capture.connect(self.reader.start_hardware_capture)
        self.sig_toggle_record_state.connect(self.reader.set_recording_state)

        self.fft_thread.started.connect(self.fft_processor.start_processing)
        self.file_thread.started.connect(self.file_writer.start_writing)

        # Pipe computational events back to UI slots
        self.fft_processor.analysis_ready.connect(self.update_analysis_ui)

        # Launch underlying loop states
        self.audio_thread.start()
        self.fft_thread.start()

    def start_always_on_pipeline(self):
        """Fires off the hardware capture and FFT rendering automatically at startup."""
        self.sig_init_capture.emit()

    def toggle_recording_action(self):
        """Toggles file writing without interrupting the active audio/FFT loops."""
        if not self.recording_active:
            # Transition to Recording
            self.recording_active = True
            self.btn_record.setText("Stop Recording (Saving to output.wav)")
            self.btn_record.setStyleSheet(
                "font-weight: bold; background-color: #F44336; color: white; padding: 6px;"
            )

            # Enable pipe routing in reader and start file system writer thread
            self.sig_toggle_record_state.emit(True)
            self.file_thread.start()
        else:
            # Transition to Idle (Still analyzing, no longer saving)
            self.recording_active = False
            self.btn_record.setText("Start Recording to File")
            self.btn_record.setStyleSheet(
                "font-weight: bold; background-color: #2196F3; color: white; padding: 6px;"
            )

            # Disable pipeline routing inside reader and tear down writer execution
            self.sig_toggle_record_state.emit(False)
            self.file_writer.stop_writing()
            self.file_thread.quit()
            self.file_thread.wait()

    @Slot(dict)
    def update_analysis_ui(self, data):
        self.progress_bar.setValue(data["level"])
        freqs = data["frequencies"]
        power = data["power_spectrum"]
        if freqs and power:
            self.spectrum_curve.setData(freqs, power)

    def closeEvent(self, event):
        # Full teardown sequence
        if self.file_thread.isRunning():
            self.file_writer.stop_writing()
            self.file_thread.quit()
            self.file_thread.wait()
            self.reader.stop_hardware_capture()
            self.fft_processor.stop_processing()
            self.audio_thread.quit()
            self.fft_thread.quit()
            self.audio_thread.wait()
            self.fft_thread.wait()

        event.accept()
