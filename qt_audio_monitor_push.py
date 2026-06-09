"""qaudiosource push mode - not working in PySide 6.11"""

import sys
import wave

import numpy as np
from PySide6.QtCore import QByteArray, QIODevice, QObject, Qt, QThread, Signal, Slot
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


# =====================================================================
# 1. ANALYSIS WORKER (Thread 2: Isolated Mathematical Telemetry)
# =====================================================================
class AnalysisWorker(QObject):
    level_calculated = Signal(float)  # Sends normalized volume to UI (0.0 to 1.0)
    spectrum_calculated = Signal(list)  # Sends visual bar arrays to UI

    def __init__(self):
        super().__init__()
        self.fft_size = 1024

    @Slot(QByteArray)
    def process_audio(self, buffer: QByteArray):
        """Executes math calculations completely isolated from UI and File I/O."""
        raw_bytes = buffer.data()
        if not raw_bytes:
            return

        try:
            # Read signed 16-bit PCM integer samples and map to float range
            samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
        except ValueError:
            return  # Drop malformed buffer boundaries cleanly

        if len(samples) == 0:
            return

        # 1. Audio Level Tracking (RMS Calculation)
        rms = np.sqrt(np.mean(samples**2))
        normalized_level = min(rms / 32768.0, 1.0)
        self.level_calculated.emit(normalized_level)

        # 2. Frequency Spectrum Tracking (FFT Calculation)
        if len(samples) >= self.fft_size:
            # Multi-threading protects the GUI from the cost of Hanning windowing + rFFT
            windowed = samples[: self.fft_size] * np.hanning(self.fft_size)
            fft_complex = np.fft.rfft(windowed)
            fft_mag = np.abs(fft_complex)

            # Normalize spectrum visualization ranges
            max_possible_val = 32768.0 * self.fft_size
            fft_norm = np.clip(fft_mag / max_possible_val * 120, 0, 1)

            # Map frequency spectrum down into 20 clean UI progress groups
            num_bars = 20
            chunks = np.array_split(fft_norm, num_bars)
            bar_values = [float(np.mean(chunk)) for chunk in chunks]
            self.spectrum_calculated.emit(bar_values)


# =====================================================================
# 2. RECORDING WORKER (Thread 3: Isolated File I/O Engine)
# =====================================================================
class RecordingWorker(QObject):
    def __init__(self, sample_rate=44100):
        super().__init__()
        self.sample_rate = sample_rate
        self.is_recording = False
        self.wav_file = None

    @Slot(str)
    def start_wav_recording(self, file_path):
        """Handles blocking file system instantiation on the recording thread."""
        self.wav_file = wave.open(file_path, "wb")
        self.wav_file.setnchannels(1)
        self.wav_file.setsampwidth(2)  # 16-bit PCM configuration
        self.wav_file.setframerate(self.sample_rate)
        self.is_recording = True

    @Slot(QByteArray)
    def write_audio_chunk(self, buffer: QByteArray):
        """Streams live bytes directly into file handle."""
        if self.is_recording and self.wav_file:
            self.wav_file.writeframes(buffer.data())

    @Slot()
    def stop_wav_recording(self):
        """Safely tears down the file streaming descriptor."""
        self.is_recording = False
        if self.wav_file:
            self.wav_file.close()
            self.wav_file = None


# =====================================================================
# 3. STREAM DISPATCHER (Pumps raw hardware captures outward)
# =====================================================================
class AudioStreamBridge(QIODevice):
    raw_data_received = Signal(QByteArray)

    def __init__(self):
        super().__init__()
        self.setOpenMode(QIODevice.WriteOnly)

    def writeData(self, data, len: int):
        """Intercepts hardware writes and relays them to worker threads."""
        if len > 0:
            self.raw_data_received.emit(QByteArray(data))
        return len


# =====================================================================
# 4. GRAPHICAL USER INTERFACE (Thread 1: UI Engine & Hardware Manager)
# =====================================================================
class MainWindow(QMainWindow):
    start_recording_signal = Signal(str)
    stop_recording_signal = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PySide6 6.11 Multi-Threaded Audio Framework")
        self.resize(550, 450)

        # Build Widget Components
        self.setup_ui()

        # Build Analysis Engine (Thread 2)
        self.analysis_thread = QThread()
        self.analysis_worker = AnalysisWorker()
        self.analysis_worker.moveToThread(self.analysis_thread)

        # Build File Storage Engine (Thread 3)
        self.recording_thread = QThread()
        self.recording_worker = RecordingWorker(sample_rate=44100)
        self.recording_worker.moveToThread(self.recording_thread)

        # Hardware Setup using Modern QtMultimedia Implementations
        self.audio_device = QMediaDevices.defaultAudioInput()
        self.audio_format = QAudioFormat()
        self.audio_format.setSampleRate(44100)
        self.audio_format.setChannelCount(1)
        self.audio_format.setSampleFormat(QAudioFormat.Int16)

        self.audio_source = QAudioSource(self.audio_device, self.audio_format)
        self.stream_bridge = AudioStreamBridge()

        # Configure Multi-Thread Routing Networks (Using QueuedConnections across boundaries)
        self.stream_bridge.raw_data_received.connect(
            self.analysis_worker.process_audio, Qt.QueuedConnection
        )
        self.stream_bridge.raw_data_received.connect(
            self.recording_worker.write_audio_chunk, Qt.QueuedConnection
        )

        # Connect Analysis metrics up to UI Thread
        self.analysis_worker.level_calculated.connect(self.update_level_display)
        self.analysis_worker.spectrum_calculated.connect(self.update_spectrum_display)

        # Connect UI Toggle triggers down to Recording Thread
        self.start_recording_signal.connect(self.recording_worker.start_wav_recording)
        self.stop_recording_signal.connect(self.recording_worker.stop_wav_recording)

        # Activate Parallel Engine Threads
        self.analysis_thread.start()
        self.recording_thread.start()

        # Open pipeline to stream microphone input data
        self.stream_bridge.open(QIODevice.WriteOnly)
        self.audio_source.start(self.stream_bridge)

    def setup_ui(self):
        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)

        # Live Volume Meter Setup
        layout.addWidget(QLabel("Live Audio Level:"))
        self.level_meter = QProgressBar()
        self.level_meter.setRange(0, 100)
        layout.addWidget(self.level_meter)

        # Dynamic Spectrum Grid Setup
        layout.addWidget(QLabel("Real-time Spectrum Analysis:"))
        self.spectrum_layout = QHBoxLayout()
        self.bar_widgets = []
        for _ in range(20):
            bar = QProgressBar()
            bar.setOrientation(Qt.Vertical)
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            self.spectrum_layout.addWidget(bar)
            self.bar_widgets.append(bar)
        layout.addLayout(self.spectrum_layout)

        # I/O Execution Toggle Controls
        self.toggle_btn = QPushButton("Start Recording")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.clicked.connect(self.on_toggle_clicked)
        layout.addWidget(self.toggle_btn)

        self.status_bar = QLabel("Status: Monitoring...")
        layout.addWidget(self.status_bar)

        self.setCentralWidget(central_widget)

    @Slot(float)
    def update_level_display(self, score):
        self.level_meter.setValue(int(score * 100))

    @Slot(list)
    def update_spectrum_display(self, array):
        for bar_widget, value in zip(self.bar_widgets, array):
            bar_widget.setValue(int(value * 100))

    def on_toggle_clicked(self, checked):
        if checked:
            self.toggle_btn.setText("Stop Recording")
            self.toggle_btn.setStyleSheet(
                "background-color: #d32f2f; color: white; font-weight: bold;"
            )
            self.status_bar.setText(
                "Status: Writing thread actively processing 'output.wav'"
            )
            self.start_recording_signal.emit("output.wav")
        else:
            self.toggle_btn.setText("Start Recording")
            self.toggle_btn.setStyleSheet("")
            self.status_bar.setText(
                "Status: Stream written to file successfully. Monitoring idle."
            )
            self.stop_recording_signal.emit()

    def closeEvent(self, event):
        """Orderly lifecycle destruction sequence preventing segmentation faults."""
        self.audio_source.stop()
        self.stream_bridge.close()
        self.stop_recording_signal.emit()

        # Safely shut down Thread 2
        self.analysis_thread.quit()
        self.analysis_thread.wait()

        # Safely shut down Thread 3
        self.recording_thread.quit()
        self.recording_thread.wait()

        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
