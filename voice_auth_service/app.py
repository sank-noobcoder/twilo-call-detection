import os
import json
import base64
import asyncio
import time

import numpy as np
import requests
import torch
import torch.nn.functional as F
import torchaudio
import webrtcvad

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from model_def import VoiceAuthenticityNet


# ========================================================
# CONFIGURATION
# ========================================================

TWILIO_ACCOUNT_SID = os.getenv(
    "TWILIO_ACCOUNT_SID"
)

TWILIO_AUTH_TOKEN = os.getenv(
    "TWILIO_AUTH_TOKEN"
)

NODE_ALERT_URL = os.getenv(
    "NODE_ALERT_URL",
    "http://localhost:5000/twilio/voice-risk-alert"
)

CHECKPOINT_PATH = os.getenv(
    "CHECKPOINT_PATH",
    "./checkpoints/best_model.pt"
)


# ========================================================
# FASTAPI
# ========================================================

app = FastAPI()


# ========================================================
# DEVICE
# ========================================================

device = torch.device(
    "cuda"
    if torch.cuda.is_available()
    else "cpu"
)

print("")
print("==========================================")
print("VOICE AUTHENTICITY DETECTOR")
print("==========================================")
print("Device:", device)
print("==========================================")
print("")


# ========================================================
# LOAD MODEL
# ========================================================

if not os.path.exists(
    CHECKPOINT_PATH
):

    raise FileNotFoundError(
        f"Checkpoint not found: {CHECKPOINT_PATH}"
    )


checkpoint = torch.load(
    CHECKPOINT_PATH,
    map_location=device
)


N_MELS = checkpoint["n_mels"]

MAX_SECONDS = checkpoint["max_seconds"]

VAL_AUC = checkpoint.get(
    "val_auc",
    None
)


model = VoiceAuthenticityNet(
    n_mels=N_MELS
).to(device)


model.load_state_dict(
    checkpoint["model_state_dict"]
)


model.eval()


print(
    "Model loaded successfully"
)

print(
    "Checkpoint:",
    CHECKPOINT_PATH
)

print(
    "N Mels:",
    N_MELS
)

print(
    "Max seconds:",
    MAX_SECONDS
)

if VAL_AUC is not None:

    print(
        "Validation AUC:",
        VAL_AUC
    )

print("")


# ========================================================
# AUDIO CONFIGURATION
# ========================================================

TWILIO_SAMPLE_RATE = 8000

MODEL_SAMPLE_RATE = 16000

CHUNK_SECONDS = 3

CHUNK_SAMPLES = (
    TWILIO_SAMPLE_RATE *
    CHUNK_SECONDS
)

# μ-law = 1 byte per sample

CHUNK_BYTES = CHUNK_SAMPLES


# ========================================================
# DETECTION CONFIGURATION
# ========================================================

HIGH_THRESHOLD = float(os.getenv("HIGH_THRESHOLD", "90"))

HIGH_REQUIRED = int(os.getenv("HIGH_REQUIRED", "3"))

MEDIUM_THRESHOLD = float(os.getenv("MEDIUM_THRESHOLD", "40"))


# ========================================================
# VAD CONFIGURATION
# ========================================================

MIN_RMS = float(os.getenv("MIN_RMS", "0.001"))

MIN_SPEECH_RATIO = float(os.getenv("MIN_SPEECH_RATIO", "0.30"))

vad = webrtcvad.Vad(2)


# ========================================================
# MEL SPECTROGRAM
# ========================================================

mel_transform = torchaudio.transforms.MelSpectrogram(

    sample_rate=
        MODEL_SAMPLE_RATE,

    n_fft=
        400,

    hop_length=
        160,

    n_mels=
        N_MELS,

    f_min=
        20,

    f_max=
        8000

)


db_transform = (
    torchaudio.transforms
    .AmplitudeToDB()
)


# ========================================================
# CALL STATE
# ========================================================

class CallState:

    def __init__(
        self,
        call_sid,
        child_call_sid=None
    ):

        self.call_sid = (
            call_sid
        )

        self.child_call_sid = (
            child_call_sid
        )

        # VERY IMPORTANT
        #
        # Detection is FALSE until
        # Node tells us the agent answered.

        self.answered = False

        self.terminated = False

        self.created_at = (
            time.time()
        )

        self.high_counts = {

            "inbound": 0,

            "outbound": 0

        }


# ========================================================
# ACTIVE CALLS
# ========================================================

active_calls = {}


# ========================================================
# PYDANTIC REQUEST
# ========================================================

class CallStatusRequest(
    BaseModel
):

    call_sid: str

    child_call_sid: str | None = None

    status: str


# ========================================================
# μ-LAW DECODER
# ========================================================

def mulaw_decode(
    mulaw_bytes: bytes
) -> np.ndarray:

    u = np.frombuffer(
        mulaw_bytes,
        dtype=np.uint8
    )

    u = np.bitwise_not(u)

    sign = u & 0x80

    exponent = (
        (u >> 4) & 0x07
    )

    mantissa = (
        u & 0x0F
    )

    magnitude = (

        (
            (
                mantissa.astype(
                    np.int32
                )
                << 3
            )
            + 132
        )
        <<
        exponent.astype(
            np.int32
        )

    )

    pcm = (
        magnitude - 132
    )

    pcm = np.where(
        sign != 0,
        -pcm,
        pcm
    )

    return (
        pcm.astype(
            np.float32
        )
        / 32768.0
    )


# ========================================================
# AUDIO INFORMATION
# ========================================================

def audio_info(
    audio: np.ndarray
):

    if (
        len(audio) == 0
    ):

        return {

            "min": 0.0,

            "max": 0.0,

            "mean": 0.0,

            "rms": 0.0

        }

    rms = float(

        np.sqrt(
            np.mean(
                audio ** 2
            )
        )

    )

    return {

        "min":
            float(
                np.min(audio)
            ),

        "max":
            float(
                np.max(audio)
            ),

        "mean":
            float(
                np.mean(audio)
            ),

        "rms":
            rms

    }


# ========================================================
# WEBRTC VAD
# ========================================================

def get_speech_ratio(
    audio_8k: np.ndarray
) -> float:

    if (
        audio_8k is None
        or len(audio_8k) == 0
    ):

        return 0.0


    audio_8k = np.asarray(
        audio_8k,
        dtype=np.float32
    )


    audio_8k = np.clip(
        audio_8k,
        -1.0,
        1.0
    )


    audio_16 = (
        audio_8k * 32767
    ).astype(
        np.int16
    )


    # 30 ms at 8 kHz

    frame_size = 240


    total_frames = 0

    speech_frames = 0


    for start in range(

        0,

        len(audio_16)
        - frame_size
        + 1,

        frame_size

    ):

        frame = audio_16[
            start:
            start + frame_size
        ]


        try:

            is_speech = (
                vad.is_speech(
                    frame.tobytes(),
                    sample_rate=8000
                )
            )


            total_frames += 1


            if is_speech:

                speech_frames += 1


        except Exception as error:

            print(
                "VAD frame error:",
                error
            )


    if total_frames == 0:

        return 0.0


    return (
        speech_frames /
        total_frames
    )


# ========================================================
# PREPARE MODEL AUDIO
# ========================================================

def prepare_audio(
    audio_8k: np.ndarray
) -> torch.Tensor:

    waveform = torch.tensor(
        audio_8k,
        dtype=torch.float32
    )


    waveform = waveform.unsqueeze(0)


    waveform = (
        torchaudio.functional
        .resample(

            waveform,

            orig_freq=
                TWILIO_SAMPLE_RATE,

            new_freq=
                MODEL_SAMPLE_RATE

        )
    )


    waveform = (
        waveform.squeeze(0)
    )


    target_samples = int(

        MAX_SECONDS *
        MODEL_SAMPLE_RATE

    )


    if (
        waveform.shape[0]
        > target_samples
    ):

        waveform = (
            waveform[
                :target_samples
            ]
        )


    elif (
        waveform.shape[0]
        < target_samples
    ):

        padding = (

            target_samples
            - waveform.shape[0]

        )

        waveform = F.pad(
            waveform,
            (0, padding)
        )


    # ====================================================
    # MEL
    # ====================================================

    mel = mel_transform(
        waveform
    )


    # ====================================================
    # DB
    # ====================================================

    mel = db_transform(
        mel
    )


    # ====================================================
    # NORMALIZATION
    # ====================================================

    mean = mel.mean()

    std = mel.std()

    mel = (
        mel - mean
    ) / (
        std + 1e-8
    )


    # Batch dimension

    mel = mel.unsqueeze(0)


    return mel


# ========================================================
# MODEL PREDICTION
# ========================================================

def predict_voice(
    audio_8k: np.ndarray
):

    model_input = (
        prepare_audio(
            audio_8k
        ).to(device)
    )


    with torch.no_grad():

        logits, _ = model(
            model_input
        )

        probabilities = (
            torch.softmax(
                logits,
                dim=1
            )
        )


    real_probability = float(
        probabilities[
            0,
            0
        ].item()
    )


    ai_probability = float(
        probabilities[
            0,
            1
        ].item()
    )


    ai_percentage = (
        ai_probability * 100.0
    )


    if (
        ai_percentage
        >= HIGH_THRESHOLD
    ):

        risk_level = "HIGH"

    elif (
        ai_percentage
        >= MEDIUM_THRESHOLD
    ):

        risk_level = "MEDIUM"

    else:

        risk_level = "LOW"


    prediction = (

        "AI"
        if ai_probability >= 0.5
        else "REAL"

    )


    return {

        "real_probability":
            real_probability,

        "ai_probability":
            ai_probability,

        "risk_score_pct":
            ai_percentage,

        "risk_level":
            risk_level,

        "prediction":
            prediction

    }


# ========================================================
# NOTIFY NODE
# ========================================================

async def notify_node(
    call_state: CallState,
    result: dict,
    track: str
):

    payload = {

        "call_sid":
            call_state.call_sid,

        "child_call_sid":
            call_state.child_call_sid,

        "risk_score_pct":
            round(
                result[
                    "risk_score_pct"
                ],
                2
            ),

        "risk_level":
            result[
                "risk_level"
            ],

        "prediction":
            result[
                "prediction"
            ],

        "track":
            track

    }


    print("")
    print("==========================================")
    print("SENDING AI ALERT TO NODE")
    print("==========================================")

    print(
        "Call:",
        call_state.call_sid
    )

    print(
        "Child:",
        call_state.child_call_sid
    )

    print(
        "Track:",
        track
    )

    print(
        "Risk:",
        result[
            "risk_score_pct"
        ]
    )

    print(
        "Prediction:",
        result[
            "prediction"
        ]
    )

    print(
        "URL:",
        NODE_ALERT_URL
    )

    print("==========================================")
    print("")


    try:

        response = await asyncio.to_thread(

            requests.post,

            NODE_ALERT_URL,

            json=payload,

            timeout=10

        )


        print(
            "Node alert response:",
            response.status_code
        )


        print(
            "Node response:",
            response.text
        )


        if (
            response.status_code
            == 200
        ):

            call_state.terminated = True

            print(
                "Node accepted alert."
            )

        else:

            print(
                "Node did not accept alert."
            )


    except Exception as error:

        print(
            "Could not notify Node:",
            error
        )


# ========================================================
# CALL STATUS
# ========================================================

@app.post("/call-status")
async def call_status(
    request: CallStatusRequest
):

    call_sid = (
        request.call_sid
    )


    print("")
    print("==========================================")
    print("CALL STATUS RECEIVED")
    print("==========================================")

    print(
        "Call:",
        call_sid
    )

    print(
        "Status:",
        request.status
    )

    print(
        "Child:",
        request.child_call_sid
    )

    print("==========================================")
    print("")


    # ====================================================
    # ANSWERED
    # ====================================================

    if (
        request.status
        == "answered"
    ):

        call_state = (
            active_calls.get(
                call_sid
            )
        )


        if call_state is None:

            call_state = CallState(
                call_sid,
                request.child_call_sid
            )

            active_calls[
                call_sid
            ] = call_state


        else:

            if (
                request.child_call_sid
            ):

                call_state.child_call_sid = (
                    request.child_call_sid
                )


        # =================================================
        # THIS ENABLES DETECTION
        # =================================================

        call_state.answered = True

        call_state.terminated = False

        call_state.high_counts = {

            "inbound": 0,

            "outbound": 0

        }


        print("")
        print("==========================================")
        print("CALL ANSWERED")
        print("==========================================")

        print(
            "Call:",
            call_sid
        )

        print(
            "Child:",
            call_state.child_call_sid
        )

        print(
            "DETECTION ENABLED"
        )

        print(
            "Inbound detection: ENABLED"
        )

        print(
            "Outbound detection: ENABLED"
        )

        print("==========================================")
        print("")


        return {

            "success":
                True,

            "detection_active":
                True

        }


    # ====================================================
    # ENDED
    # ====================================================

    if (
        request.status
        == "ended"
    ):

        call_state = (
            active_calls.get(
                call_sid
            )
        )


        if call_state:

            call_state.answered = False

            call_state.high_counts = {

                "inbound": 0,

                "outbound": 0

            }


        print("")
        print("==========================================")
        print("CALL ENDED")
        print("==========================================")

        print(
            "Call:",
            call_sid
        )

        print(
            "DETECTION DISABLED"
        )

        print("==========================================")
        print("")


        active_calls.pop(
            call_sid,
            None
        )


        return {

            "success":
                True,

            "detection_active":
                False

        }


    return {
        "success": True
    }


# ========================================================
# HEALTH
# ========================================================

@app.get("/health")
async def health():

    return {

        "status":
            "ok",

        "model_loaded":
            True,

        "active_calls":
            len(active_calls)

    }


# ========================================================
# MEDIA STREAM
# ========================================================

@app.websocket(
    "/media-stream"
)
async def media_stream(
    websocket: WebSocket
):

    await websocket.accept()


    call_sid = None

    stream_sid = None


    buffers = {

        "inbound":
            bytearray(),

        "outbound":
            bytearray()

    }


    stream_started_at = (
        time.time()
    )


    print("")
    print("==========================================")
    print("MEDIA WEBSOCKET CONNECTED")
    print("==========================================")
    print("Waiting for Twilio start event...")
    print("==========================================")
    print("")


    try:

        while True:

            message = (
                await websocket
                .receive_text()
            )


            data = json.loads(
                message
            )


            event = data.get(
                "event"
            )


            # =================================================
            # START
            # =================================================

            if event == "start":

                start = data.get(
                    "start",
                    {}
                )


                call_sid = (
                    start.get(
                        "callSid"
                    )
                )


                stream_sid = (
                    start.get(
                        "streamSid"
                    )
                )


                tracks = (
                    start.get(
                        "tracks",
                        []
                    )
                )


                media_format = (
                    start.get(
                        "mediaFormat",
                        {}
                    )
                )


                stream_started_at = (
                    time.time()
                )


                print("")
                print("==========================================")
                print("MEDIA STREAM STARTED")
                print("==========================================")

                print(
                    "Call:",
                    call_sid
                )

                print(
                    "Stream:",
                    stream_sid
                )

                print(
                    "Tracks:",
                    tracks
                )

                print(
                    "Encoding:",
                    media_format.get(
                        "encoding"
                    )
                )

                print(
                    "Sample rate:",
                    media_format.get(
                        "sampleRate"
                    )
                )

                print(
                    "Channels:",
                    media_format.get(
                        "channels"
                    )
                )

                print("")
                print(
                    "IMPORTANT:"
                )

                print(
                    "Detection will NOT start"
                )

                print(
                    "until agent answers."
                )

                print("==========================================")
                print("")


                if (
                    call_sid
                    not in active_calls
                ):

                    active_calls[
                        call_sid
                    ] = CallState(
                        call_sid
                    )


                continue


            # =================================================
            # MEDIA
            # =================================================

            if event == "media":

                if not call_sid:

                    continue


                media = data.get(
                    "media",
                    {}
                )


                track = media.get("track")

                # Twilio normally sends "inbound"/"outbound" here, but
                # normalize the suffixed form too so valid audio is not
                # silently discarded when the stream format changes.
                if track in ("inbound_track", "outbound_track"):
                    track = track.removesuffix("_track")


                payload = media.get(
                    "payload"
                )


                if track not in ("inbound", "outbound"):

                    print(
                        "Ignoring media event with unsupported track:",
                        track
                    )

                    continue


                if not payload:

                    continue


                call_state = (
                    active_calls.get(
                        call_sid
                    )
                )


                if call_state is None:

                    call_state = CallState(
                        call_sid
                    )

                    active_calls[
                        call_sid
                    ] = call_state


                # =================================================
                # CRITICAL PROTECTION
                # =================================================
                #
                # Media Stream begins BEFORE agent answers.
                #
                # We DO NOT buffer or analyze anything until
                # call_state.answered == True.
                #
                # This completely removes pre-answer audio
                # from the detection pipeline.
                # =================================================

                if not call_state.answered:

                    # Throw away audio before answer.

                    continue


                # =================================================
                # CHECK TERMINATION
                # =================================================

                if call_state.terminated:

                    continue


                # =================================================
                # DECODE BASE64
                # =================================================

                try:

                    raw_audio = (
                        base64.b64decode(
                            payload
                        )
                    )

                except Exception as error:

                    print(
                        "Base64 decode error:",
                        error
                    )

                    continue


                # =================================================
                # ADD AUDIO TO BUFFER
                # =================================================

                buffers[
                    track
                ].extend(
                    raw_audio
                )


                # =================================================
                # PROCESS 3 SECOND CHUNKS
                # =================================================

                while (

                    len(
                        buffers[track]
                    )
                    >= CHUNK_BYTES

                ):

                    chunk = bytes(

                        buffers[
                            track
                        ][
                            :CHUNK_BYTES
                        ]

                    )


                    del buffers[
                        track
                    ][
                        :CHUNK_BYTES
                    ]


                    # =================================================
                    # μ-LAW → PCM
                    # =================================================

                    audio_8k = (
                        mulaw_decode(
                            chunk
                        )
                    )


                    # =================================================
                    # AUDIO INFO
                    # =================================================

                    info = audio_info(
                        audio_8k
                    )


                    elapsed = (

                        time.time()
                        - stream_started_at

                    )


                    print("")
                    print("------------------------------------------")
                    print("AUDIO CHUNK")
                    print("------------------------------------------")

                    print(
                        "Track:",
                        track
                    )

                    print(
                        "Call:",
                        call_sid
                    )

                    print(
                        "Answered:",
                        call_state.answered
                    )

                    print(
                        "Stream time:",
                        f"{elapsed:.2f}",
                        "seconds"
                    )

                    print(
                        "Audio time:",
                        CHUNK_SECONDS,
                        "seconds"
                    )

                    print(
                        "Samples:",
                        len(audio_8k)
                    )

                    print(
                        "RMS:",
                        f"{info['rms']:.8f}"
                    )

                    print("------------------------------------------")


                    # =================================================
                    # EXTRA SAFETY
                    # =================================================

                    if not call_state.answered:

                        print(
                            "CALL IS NOT ANSWERED."
                        )

                        print(
                            "Skipping detection."
                        )

                        call_state.high_counts[
                            track
                        ] = 0

                        continue


                    # =================================================
                    # RMS GATE
                    # =================================================

                    if (
                        info["rms"]
                        < MIN_RMS
                    ):

                        print(
                            "Low energy audio."
                        )

                        print(
                            "Skipping model."
                        )

                        call_state.high_counts[
                            track
                        ] = 0

                        continue


                    # =================================================
                    # WEBRTC VAD
                    # =================================================

                    speech_ratio = (
                        get_speech_ratio(
                            audio_8k
                        )
                    )


                    speech_percentage = (
                        speech_ratio * 100.0
                    )


                    print(
                        "Speech ratio:",
                        f"{speech_percentage:.2f}%"
                    )


                    # =================================================
                    # NO SPEECH
                    # =================================================

                    if (
                        speech_ratio
                        < MIN_SPEECH_RATIO
                    ):

                        print(
                            "NO SPEECH DETECTED"
                        )

                        print(
                            "Skipping model."
                        )

                        call_state.high_counts[
                            track
                        ] = 0

                        continue


                    # =================================================
                    # SPEECH
                    # =================================================

                    print(
                        "SPEECH DETECTED"
                    )

                    print(
                        "Running AI voice model..."
                    )


                    # =================================================
                    # MODEL
                    # =================================================

                    try:

                        result = (
                            predict_voice(
                                audio_8k
                            )
                        )

                    except Exception as error:

                        print(
                            "Model prediction error:",
                            error
                        )

                        continue


                    # =================================================
                    # RESULT
                    # =================================================

                    print("")
                    print("==========================================")
                    print("MODEL RESULT")
                    print("==========================================")

                    print(
                        "Track:",
                        track
                    )

                    print(
                        "Call:",
                        call_sid
                    )

                    print(
                        "Prediction:",
                        result[
                            "prediction"
                        ]
                    )

                    print(
                        "AI probability:",
                        f"{result['ai_probability'] * 100:.2f}%"
                    )

                    print(
                        "Real probability:",
                        f"{result['real_probability'] * 100:.2f}%"
                    )

                    print(
                        "Risk:",
                        f"{result['risk_score_pct']:.2f}%"
                    )

                    print(
                        "Risk level:",
                        result[
                            "risk_level"
                        ]
                    )

                    print("==========================================")
                    print("")


                    # =================================================
                    # LOW
                    # =================================================

                    if (
                        result[
                            "risk_level"
                        ]
                        == "LOW"
                    ):

                        call_state.high_counts[
                            track
                        ] = 0

                        print(
                            "LOW RISK"
                        )

                        print(
                            "HIGH counter reset."
                        )

                        continue


                    # =================================================
                    # MEDIUM
                    # =================================================

                    if (
                        result[
                            "risk_level"
                        ]
                        == "MEDIUM"
                    ):

                        call_state.high_counts[
                            track
                        ] = 0

                        print(
                            "MEDIUM RISK"
                        )

                        print(
                            "HIGH counter reset."
                        )

                        continue


                    # =================================================
                    # HIGH
                    # =================================================

                    if (
                        result[
                            "risk_level"
                        ]
                        == "HIGH"
                    ):

                        call_state.high_counts[
                            track
                        ] += 1


                        print("")
                        print(
                            "HIGH RISK COUNT:",
                            call_state.high_counts[
                                track
                            ],
                            "/",
                            HIGH_REQUIRED
                        )

                        print(
                            "Track:",
                            track
                        )

                        print(
                            "Threshold:",
                            HIGH_THRESHOLD,
                            "%"
                        )

                        print("")


                        # =================================================
                        # CONFIRMED AI
                        # =================================================

                        if (

                            call_state.high_counts[
                                track
                            ]
                            >= HIGH_REQUIRED

                        ):

                            print("")
                            print("==========================================")
                            print("AI VOICE CONFIRMED")
                            print("==========================================")

                            print(
                                "Call:",
                                call_sid
                            )

                            print(
                                "Track:",
                                track
                            )

                            print(
                                "AI probability:",
                                f"{result['ai_probability'] * 100:.2f}%"
                            )

                            print(
                                "Risk:",
                                f"{result['risk_score_pct']:.2f}%"
                            )

                            print(
                                "Confirmed HIGH detections:",
                                HIGH_REQUIRED
                            )

                            print(
                                "Terminating entire call"
                            )

                            print("==========================================")
                            print("")


                            call_state.terminated = True


                            await notify_node(

                                call_state,

                                result,

                                track

                            )


                            break


            # =================================================
            # STOP
            # =================================================

            elif event == "stop":

                print("")
                print("==========================================")
                print("MEDIA STREAM STOPPED")
                print("==========================================")

                print(
                    "Call:",
                    call_sid
                )

                print(
                    "Stream:",
                    stream_sid
                )

                print("==========================================")
                print("")

                break


    except WebSocketDisconnect:

        print("")
        print(
            "Media WebSocket disconnected:",
            call_sid
        )
        print("")


    except Exception as error:

        print("")
        print(
            "Media stream error:",
            error
        )
        print("")


    finally:

        print(
            "Media stream closed for call:",
            call_sid
        )
