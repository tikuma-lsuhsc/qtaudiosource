import sys
import wave

import numpy as np
from PySide6.QtCore import (
    QByteArray,
    QIODevice,
    QObject,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
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
# 1. ANALYSIS WORKER (Thread 2: Mathematical Telemetry)
# =====================================================================
class AnalysisWorker(QObject):
    level_calculated = Signal(float)
    spectrum_calculated = Signal(list)

    def __init__(self):
        super().__init__()
        self.fft_size = 1024

    @Slot(QByteArray)
    def process_audio(self, buffer: QByteArray):
        raw_bytes = buffer.data()
        if not raw_bytes:
            return

        try:
            samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
        except ValueError:
            return

        if len(samples) == 0:
            return

        # 1. Audio Level (RMS)
        rms = np.sqrt(np.mean(samples**2))
        normalized_level = min(rms / 32768.0, 1.0)
        self.level_calculated.emit(normalized_level)

        # 2. Frequency Spectrum (FFT)
        if len(samples) >= self.fft_size:
            windowed = samples[: self.fft_size] * np.hanning(self.fft_size)
            fft_complex = np.fft.rfft(windowed)
            fft_mag = np.abs(fft_complex)

            max_possible_val = 32768.0 * self.fft_size
            fft_norm = np.clip(fft_mag / max_possible_val * 120, 0, 1)

            num_bars = 20
            chunks = np.array_split(fft_norm, num_bars)
            bar_values = [float(np.mean(chunk)) for chunk in chunks]
            self.spectrum_calculated.emit(bar_values)


# =====================================================================
# 2. RECORDING WORKER (Thread 3: File I/O Engine)
# =====================================================================
class RecordingWorker(QObject):
    def __init__(self, sample_rate=44100):
        super().__init__()
        self.sample_rate = sample_rate
        self.is_recording = False
        self.wav_file = None

    @Slot(str)
    def start_wav_recording(self, file_path):
        self.wav_file = wave.open(file_path, "wb")
        self.wav_file.setnchannels(1)
        self.wav_file.setsampwidth(2)
        self.wav_file.setframerate(self.sample_rate)
        self.is_recording = True

    @Slot(QByteArray)
    def write_audio_chunk(self, buffer: QByteArray):
        if self.is_recording and self.wav_file:
            self.wav_file.writeframes(buffer.data())

    @Slot()
    def stop_wav_recording(self):
        self.is_recording = False
        if self.wav_file:
            self.wav_file.close()
            self.wav_file = None


# =====================================================================
# 3. PULL-MODE STREAM BUFFER (Must accept ReadWrite OpenMode)
# =====================================================================
class AudioPullBridge(QIODevice):
    def __init__(self, source: QIODevice):
        super().__init__()

        # Must support ReadWrite internally so hardware writes and timer reads coexist
        self.setOpenMode(QIODevice.ReadWrite)
        self._buffer = bytearray()

        def buffer():
            len = self.m_audio_input.bytesAvailable()
            buffer_size = 4096
            if len > buffer_size:
                len = buffer_size
            buffer: QByteArray = self.source.read(len)
            if len > 0:
                level = self.m_audio_info.calculate_level(buffer, len)
                self.m_canvas.set_level(level)

    # def writeData(self, data, len: int):
    #     """Hardware pushes audio data into this internal buffer."""
    #     self._buffer.extend(data)
    #     return len

    def readData(self, maxlen):
        """UI Application Pull timer pulls data out of this buffer."""
        chunk_size = min(maxlen, len(self._buffer))
        if chunk_size <= 0:
            return b""

        data = self._buffer[:chunk_size]
        del self._buffer[:chunk_size]  # Flush consumed buffer space
        return bytes(data)

    def bytesAvailable(self):
        return len(self._buffer) + super().bytesAvailable()


# =====================================================================
# 4. GRAPHICAL USER INTERFACE (Thread 1: UI Engine & Hardware Manager)
# =====================================================================
class MainWindow(QMainWindow):
    start_recording_signal = Signal(str)
    stop_recording_signal = Signal()
    raw_data_received = Signal(QByteArray)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PySide6 6.11 Audio Framework (Pull Mode Fixed)")
        self.resize(550, 450)

        self.setup_ui()

        # Build Threads
        self.analysis_thread = QThread()
        self.analysis_worker = AnalysisWorker()
        self.analysis_worker.moveToThread(self.analysis_thread)

        self.recording_thread = QThread()
        self.recording_worker = RecordingWorker(sample_rate=44100)
        self.recording_worker.moveToThread(self.recording_thread)

        # Hardware Setup
        self.audio_device = QMediaDevices.defaultAudioInput()
        self.audio_format = QAudioFormat()
        self.audio_format.setSampleRate(44100)
        self.audio_format.setChannelCount(1)
        self.audio_format.setSampleFormat(QAudioFormat.Int16)

        self.audio_source = QAudioSource(self.audio_device, self.audio_format)

        # Wire Signals
        self.raw_data_received.connect(
            self.analysis_worker.process_audio, Qt.QueuedConnection
        )
        self.raw_data_received.connect(
            self.recording_worker.write_audio_chunk, Qt.QueuedConnection
        )

        self.analysis_worker.level_calculated.connect(self.update_level_display)
        self.analysis_worker.spectrum_calculated.connect(self.update_spectrum_display)

        self.start_recording_signal.connect(self.recording_worker.start_wav_recording)
        self.stop_recording_signal.connect(self.recording_worker.stop_wav_recording)

        self.analysis_thread.start()
        self.recording_thread.start()

        # FIXED: Initialize as ReadWrite so QAudioSource can push, and we can pull
        io = self.audio_source.start(self.stream_bridge)
        self.stream_bridge = AudioPullBridge(io)
        io.readyRead.connect(push_mode_slot)

        # Setup high-resolution pulling polling timer
        self.pull_timer = QTimer(self)
        self.pull_timer.setInterval(20)
        self.pull_timer.timeout.connect(self.pull_audio_data)
        self.pull_timer.start()

    def pull_audio_data(self):
        """Manually pulls bytes out of the stream bridge to broadcast across threads."""
        available_bytes = self.stream_bridge.bytesAvailable()
        if available_bytes > 0:
            raw_chunk = self.stream_bridge.read(available_bytes)
            if raw_chunk:
                self.raw_data_received.emit(QByteArray(raw_chunk))

    def setup_ui(self):
        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)

        layout.addWidget(QLabel("Live Audio Level:"))
        self.level_meter = QProgressBar()
        self.level_meter.setRange(0, 100)
        layout.addWidget(self.level_meter)

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

        self.toggle_btn = QPushButton("Start Recording")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.clicked.connect(self.on_toggle_clicked)
        layout.addWidget(self.toggle_btn)

        self.status_bar = QLabel("Status: Monitoring in Pull Mode...")
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
                "Status: Recording actively streaming via background thread."
            )
            self.start_recording_signal.emit("output.wav")
        else:
            self.toggle_btn.setText("Start Recording")
            self.toggle_btn.setStyleSheet("")
            self.status_bar.setText("Status: Recording finalized. Monitoring idle.")
            self.stop_recording_signal.emit()

    def closeEvent(self, event):
        self.pull_timer.stop()
        self.audio_source.stop()
        self.stream_bridge.close()
        self.stop_recording_signal.emit()

        self.analysis_thread.quit()
        self.analysis_thread.wait()

        self.recording_thread.quit()
        self.recording_thread.wait()

        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
