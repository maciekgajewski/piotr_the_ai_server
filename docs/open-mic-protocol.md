# Open-Mic Streaming Protocol

Open-mic mode is a separate microphone conversation layer. It does not change the
old wake-word and follow-up protocol used by microphones without `open_mic: true`.

The goal is to accept complete spoken sentences that begin with the configured
wake phrase, without waiting for a second post-wake recording cue.

## Runtime Protocol

1. `StartOpenMicListening`

   The microphone manager sends this control event only for microphones configured
   with `open_mic: true`. The driver starts continuous capture without local
   wake-word detection.

2. `SpeechSegmentStarted`

   The driver emits `AudioStart` and then forwards non-silence `AudioChunk`
   events. Local audio gating remains owned by the microphone driver.

3. `TranscriptPartial`

   The open-mic layer sends incoming audio to streaming STT. STT emits rolling
   partial transcript snapshots. These partials are implementation-private:

   - they may be used for wake-phrase detection
   - they must not be sent to the agent
   - they must not be logged as transcript text

4. `WakePhraseCandidateDetected`

   If a partial transcript contains the configured wake phrase, the current
   speech segment is marked as relevant. This is internal state only. The system
   must not play a chime at this point, because the user may still be speaking.

5. `SpeechSegmentEnded`

   The driver emits `AudioEnd` after end-of-speech detection.

6. `UtteranceAccepted`

   If the segment has a wake-phrase candidate, the manager accepts the complete
   segment and plays the accepted-utterance cue. This is the former wake-word
   chime semantics moved to the end of the sentence: it means "the full utterance
   was heard and is now being processed."

   If no partial contained the wake phrase, the manager may run one final private
   transcript pass to avoid missing short segments. If the final transcript still
   has no wake phrase, the segment is discarded silently.

7. `TranscriptFinal`

   After acceptance, STT produces a final transcript for the accepted segment.
   The manager extracts the text following the wake phrase. Only accepted final
   text may be logged or forwarded.

8. `SpeakerRecognitionFinal`

   Speaker recognition runs only for accepted audio. It may use audio buffered
   during the speech segment, but its result is awaited only after acceptance.

9. Agent input

   The manager sends the normal conversation events downstream:

   - `NewConversation`
   - `MessageBegin`
   - `MessageFragment`
   - `MessageEnd`

## Streaming STT Design

Faster Whisper is not a token-streaming PCM recognizer, so open-mic streaming is
implemented with rolling-window transcription:

- audio chunks are appended continuously while speech is active
- partial STT jobs transcribe the latest rolling window at a configured interval
- partial jobs use a small beam size for responsiveness
- the final pass transcribes the accepted segment with the normal final beam size

The STT implementation logs model, audio seconds, duration, and transcript
lengths. It must not log partial or final transcript text. Open-mic transcript
text may only be logged by the microphone layer after `UtteranceAccepted`.

## Backlog Handling

Each partial result includes the audio timestamp covered by that STT job. The
streaming STT layer compares that timestamp with the newest audio timestamp. If
the gap grows beyond the configured backlog threshold, it logs a warning and
skips stale work by using the newest rolling window on the next pass.

This keeps open mic from accumulating a long queue of obsolete partial
transcriptions when the model is slower than real time.
