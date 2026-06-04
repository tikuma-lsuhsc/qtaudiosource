// Copyright (C) 2026 The Qt Company Ltd.
// SPDX-License-Identifier: LicenseRef-Qt-Commercial OR BSD-3-Clause

#ifndef AUDIORECORDER_H
#define AUDIORECORDER_H

#include <QIODevice>
#include <QAudioSource>

#define DR_WAV_IMPLEMENTATION
#define DRWAV_API static
#define DRWAV_PRIVATE static
#define DR_WAV_NO_CONVERSION_API
#define DR_WAV_NO_WCHAR
#include "dr_libs/dr_wav.h"

#include <QDebug>

namespace
{

    using namespace std::chrono_literals;
    constexpr auto recordingDuration = 5s;

} // namespace

class AudioRecorder
{
public:
    AudioRecorder(const QAudioFormat &format, QString fileName) : m_bytesExpected(
                                                                      format.bytesForDuration(std::chrono::microseconds(recordingDuration).count())),
                                                                  m_fileName(fileName)
    {
        // Prepare WAV file
        drwav_data_format wavFormat;
        wavFormat.container = drwav_container_w64;
        wavFormat.format = (format.sampleFormat() == QAudioFormat::Float) ? DR_WAVE_FORMAT_IEEE_FLOAT
                                                                          : DR_WAVE_FORMAT_PCM;
        wavFormat.channels = format.channelCount();
        wavFormat.sampleRate = format.sampleRate();
        wavFormat.bitsPerSample = format.bytesPerSample() * 8;

        if (drwav_init_file_write(&m_wav, m_fileName.toLatin1().constData(), &wavFormat, nullptr) == 0)
        {
            qWarning() << "Error opening WAV file for writing:" << m_fileName;
        }
    }

    virtual ~AudioRecorder()
    {
        auto result = drwav_uninit(&m_wav);
        if (result != DRWAV_SUCCESS)
            qWarning() << "Failed to close WAV file:" << result;
        else
            qInfo() << "Wav file written:" << m_fileName;
    }

    [[nodiscard]] virtual int progress(int maximum) const
    {
        return maximum * qMin(bytesWritten(), m_bytesExpected) / m_bytesExpected;
    }

    [[nodiscard]] virtual bool isDone() const
    {
        return bytesWritten() >= m_bytesExpected;
    }

protected:
    qint64 writeBytesToFile(QSpan<const std::byte> buffer)
    {
        qint64 bytesPerFrame = m_wav.channels * m_wav.bitsPerSample / 8;
        if (bytesPerFrame == 0)
            return 0;
        qint64 framesOnBuffer = buffer.size() / bytesPerFrame;
        quint64 framesWritten = drwav_write_pcm_frames(&m_wav, framesOnBuffer, buffer.data());
        return qint64(framesWritten) * bytesPerFrame;
    }

    [[nodiscard]] virtual quint64 bytesWritten() const = 0;

    quint64 m_bytesExpected;

private:
    drwav m_wav;
    QString m_fileName;
};

// Recorder in pull mode
class PullRecorder : public AudioRecorder
{
public:
    using AudioRecorder::AudioRecorder;

    // Call writeSpan from main thread
    // This writes directly to file
    void writeSpan(QSpan<const std::byte> data)
    {
        m_bytesWritten += writeBytesToFile(data);
    }

protected:
    quint64 bytesWritten() const override
    {
        return m_bytesWritten;
    }

private:
    quint64 m_bytesWritten = 0;
};

// Recorder in push mode
class PushRecorder : public QIODevice, public AudioRecorder
{
public:
    using AudioRecorder::AudioRecorder;

protected:
    qint64 readData(char *, qint64) override { return 0; };
    qint64 writeData(const char *data, qint64 len) override
    {
        auto bytesWritten =
            writeBytesToFile(QSpan{reinterpret_cast<const std::byte *>(data), (qsizetype)len});
        m_bytesWritten += bytesWritten;
        return bytesWritten;
    }

    quint64 bytesWritten() const override
    {
        return m_bytesWritten;
    };

private:
    quint64 m_bytesWritten = 0;
};

// Recorder in callback mode
class CallbackRecorder : public AudioRecorder
{
public:
    CallbackRecorder(const QAudioFormat &format, QString fileName) : AudioRecorder(format, fileName)
    {
        // Pre-allocate a 5s buffer
        m_recordingBuffer.resize(m_bytesExpected, std::byte(0));
        m_recordingIterator = m_recordingBuffer.begin();
    }

    ~CallbackRecorder()
    {
        writeBytesToFile(m_recordingBuffer);
    }

    // Call writeSpan from audio callback thread
    // This writes data to a pre-allocated buffer
    void writeSpan(QSpan<const std::byte> data) noexcept
#if defined(__has_cpp_attribute) && __has_cpp_attribute(clang::nonblocking)
        [[clang::nonblocking]]
#endif
    {
        auto bytesToWrite = m_bytesExpected - bytesWritten();
        auto length = qMin(bytesToWrite, quint64(data.size()));
        if (length == 0)
            return;

        m_recordingIterator = std::copy_n(data.begin(), length, m_recordingIterator);
        m_bytesWritten.store(std::distance(m_recordingBuffer.begin(), m_recordingIterator),
                             std::memory_order_relaxed);
    }

protected:
    [[nodiscard]] quint64 bytesWritten() const override
    {
        return m_bytesWritten.load(std::memory_order_relaxed);
    }

private:
    std::vector<std::byte> m_recordingBuffer;
    std::vector<std::byte>::iterator m_recordingIterator;
    std::atomic<quint64> m_bytesWritten = 0;
};

#endif
