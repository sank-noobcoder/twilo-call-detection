require("dotenv").config();

const express = require("express");
const twilio = require("twilio");

const app = express();

/*
========================================================
MIDDLEWARE
========================================================
*/

app.use(express.urlencoded({ extended: false }));
app.use(express.json());
app.use(express.static("public"));

const PORT = process.env.PORT || 5000;

/*
========================================================
TWILIO CONFIGURATION
========================================================
*/

const TWILIO_ACCOUNT_SID =
    process.env.TWILIO_ACCOUNT_SID;

const TWILIO_AUTH_TOKEN =
    process.env.TWILIO_AUTH_TOKEN;

const TWILIO_API_KEY =
    process.env.TWILIO_API_KEY;

const TWILIO_API_SECRET =
    process.env.TWILIO_API_SECRET;

const TWILIO_PHONE_NUMBER =
    process.env.TWILIO_PHONE_NUMBER;

const AGENT_PHONE_NUMBER =
    process.env.AGENT_PHONE_NUMBER;

const TWIML_APP_SID =
    process.env.TWIML_APP_SID;

const MEDIA_STREAM_WS_URL =
    process.env.MEDIA_STREAM_WS_URL;

const NODE_PUBLIC_URL =
    process.env.NODE_PUBLIC_URL;

const PYTHON_STATUS_URL =
    process.env.PYTHON_STATUS_URL ||
    "http://localhost:8000/call-status";

/*
========================================================
TWILIO CLIENT
========================================================
*/

const client = twilio(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN
);

/*
========================================================
ACTIVE CALL STORAGE
========================================================

parentCallSid:
    Browser -> Twilio parent call

childCallSid:
    Twilio -> Agent child call

answered:
    Agent has answered

terminated:
    AI voice detected and call terminated

========================================================
*/

const activeCalls = new Map();

/*
========================================================
HEALTH CHECK
========================================================
*/

app.get("/health", (req, res) => {
    res.json({
        status: "ok",
        service: "twilio-node-server",
        active_calls: activeCalls.size
    });
});

/*
========================================================
TWILIO VOICE WEBHOOK
========================================================

Twilio calls this endpoint when the browser starts
the phone call.

IMPORTANT:

There is NO <Say> here.

We do not want Twilio-generated synthetic voice
to enter the AI detection pipeline.

========================================================
*/

app.post("/twilio/voice", (req, res) => {
    try {
        const VoiceResponse =
            twilio.twiml.VoiceResponse;

        const response =
            new VoiceResponse();

        const parentCallSid =
            req.body.CallSid;

        const from =
            req.body.From;

        console.log("");
        console.log("==========================================");
        console.log("INCOMING CALL");
        console.log("==========================================");
        console.log("From:", from);
        console.log("Parent Call SID:", parentCallSid);
        console.log("==========================================");
        console.log("");

        /*
        ------------------------------------------------
        VALIDATE CALL SID
        ------------------------------------------------
        */

        if (!parentCallSid) {
            console.error(
                "ERROR: CallSid missing from incoming Twilio request."
            );

            return res
                .status(400)
                .send("Missing CallSid");
        }

        /*
        ------------------------------------------------
        CREATE CALL STATE
        ------------------------------------------------
        */

        activeCalls.set(parentCallSid, {
            parentCallSid: parentCallSid,
            childCallSid: null,
            answered: false,
            terminated: false,
            from: from || null,
            to: AGENT_PHONE_NUMBER
        });

        /*
        ------------------------------------------------
        START MEDIA STREAM
        ------------------------------------------------

        Stream starts immediately.

        Python will IGNORE audio until the agent
        answers.

        ------------------------------------------------
        */

        if (!MEDIA_STREAM_WS_URL) {
            throw new Error(
                "MEDIA_STREAM_WS_URL is missing from .env"
            );
        }

        const start =
            response.start();

        start.stream({
            url: MEDIA_STREAM_WS_URL,
            track: "both_tracks"
        });

        /*
        ------------------------------------------------
        DIAL AGENT
        ------------------------------------------------

        IMPORTANT:

        The status callback belongs to <Number>,
        not <Dial>.

        Twilio sends:

        CallSid
            = child/agent call SID

        ParentCallSid
            = original/browser parent call SID

        CallStatus
            = initiated
            = ringing
            = in-progress
            = completed

        ------------------------------------------------
        */

        const publicNodeUrl =
            getPublicNodeUrl();

        const dial =
            response.dial({
                callerId:
                    TWILIO_PHONE_NUMBER,
                timeout: 30
            });

        dial.number(
            {
                statusCallback:
                    `${publicNodeUrl}/twilio/dial-status`,

                statusCallbackMethod:
                    "POST",

                statusCallbackEvent:
                    "initiated ringing answered completed"
            },

            AGENT_PHONE_NUMBER
        );

        /*
        ------------------------------------------------
        GENERATED TWIML
        ------------------------------------------------
        */

        const twiml =
            response.toString();

        console.log("");
        console.log("Generated TwiML:");
        console.log(twiml);
        console.log("");

        res
            .type("text/xml")
            .send(twiml);

    } catch (error) {

        console.error("");
        console.error("==========================================");
        console.error("VOICE WEBHOOK ERROR");
        console.error("==========================================");
        console.error(error);
        console.error("==========================================");
        console.error("");

        res
            .status(500)
            .send("Voice webhook error");
    }
});

/*
========================================================
DIAL STATUS CALLBACK
========================================================

IMPORTANT:

For <Dial><Number>:

req.body.CallSid
    = CHILD / AGENT CALL SID

req.body.ParentCallSid
    = PARENT / BROWSER CALL SID

req.body.CallStatus
    = initiated
    = ringing
    = in-progress
    = completed
    = busy
    = no-answer
    = failed
    = canceled

"in-progress" means the agent answered.

========================================================
*/

app.post(
    "/twilio/dial-status",
    async (req, res) => {

        try {

            console.log("");
            console.log("==========================================");
            console.log(
                "!!! TWILIO DIAL STATUS CALLBACK RECEIVED !!!"
            );
            console.log("==========================================");

            console.log(
                "FULL CALLBACK BODY:"
            );

            console.log(req.body);

            console.log("------------------------------------------");

            /*
            ------------------------------------------------
            GET CORRECT TWILIO VALUES
            ------------------------------------------------
            */

            const childCallSid =
                req.body.CallSid || null;

            const parentCallSid =
                req.body.ParentCallSid || null;

            const callStatus =
                req.body.CallStatus || null;

            const sequenceNumber =
                req.body.SequenceNumber || null;

            console.log(
                "Child Call SID:",
                childCallSid
            );

            console.log(
                "Parent Call SID:",
                parentCallSid
            );

            console.log(
                "Call Status:",
                callStatus
            );

            console.log(
                "Sequence Number:",
                sequenceNumber
            );

            console.log(
                "=========================================="
            );

            /*
            ------------------------------------------------
            VALIDATE PARENT SID
            ------------------------------------------------
            */

            if (!parentCallSid) {

                console.error(
                    "ERROR: ParentCallSid missing."
                );

                return res.sendStatus(200);
            }

            /*
            ------------------------------------------------
            FIND ACTIVE CALL
            ------------------------------------------------
            */

            let callInfo =
                activeCalls.get(
                    parentCallSid
                );

            /*
            ------------------------------------------------
            CREATE STATE IF MISSING
            ------------------------------------------------
            */

            if (!callInfo) {

                console.log(
                    "Call state missing."
                );

                console.log(
                    "Creating call state..."
                );

                callInfo = {
                    parentCallSid:
                        parentCallSid,

                    childCallSid:
                        childCallSid,

                    answered:
                        false,

                    terminated:
                        false,

                    from:
                        null,

                    to:
                        AGENT_PHONE_NUMBER
                };

                activeCalls.set(
                    parentCallSid,
                    callInfo
                );
            }

            /*
            ------------------------------------------------
            SAVE CHILD CALL SID
            ------------------------------------------------
            */

            if (childCallSid) {

                callInfo.childCallSid =
                    childCallSid;

                activeCalls.set(
                    parentCallSid,
                    callInfo
                );

                console.log(
                    "Child Call SID saved:",
                    childCallSid
                );
            }

            /*
            =================================================
            INITIATED
            =================================================
            */

            if (
                callStatus === "initiated"
            ) {

                console.log("");
                console.log(
                    "AGENT CALL INITIATED"
                );

                console.log(
                    "AI DETECTION: OFF"
                );

                console.log("");
            }

            /*
            =================================================
            RINGING
            =================================================
            */

            else if (
                callStatus === "ringing"
            ) {

                console.log("");
                console.log(
                    "AGENT PHONE IS RINGING"
                );

                console.log(
                    "AI DETECTION: OFF"
                );

                console.log("");
            }

            /*
            =================================================
            ANSWERED
            =================================================

            IMPORTANT:

            Twilio sends:

                CallStatus = "in-progress"

            when the agent answers.

            =================================================
            */

            else if (
                callStatus === "answered" ||
                callStatus === "in-progress"
            ) {

                console.log("");
                console.log("==========================================");
                console.log("CALL ANSWERED");
                console.log("==========================================");

                console.log(
                    "Parent:",
                    parentCallSid
                );

                console.log(
                    "Child:",
                    childCallSid
                );

                console.log(
                    "AI DETECTION IS NOW ACTIVE"
                );

                console.log(
                    "Both inbound + outbound tracks enabled"
                );

                console.log(
                    "=========================================="
                );

                /*
                ------------------------------------------------
                UPDATE CALL STATE
                ------------------------------------------------
                */

                callInfo.childCallSid =
                    childCallSid;

                callInfo.answered =
                    true;

                activeCalls.set(
                    parentCallSid,
                    callInfo
                );

                /*
                ------------------------------------------------
                NOTIFY PYTHON
                ------------------------------------------------
                */

                const pythonResult =
                    await notifyPythonCallStatus(
                        parentCallSid,
                        childCallSid,
                        "answered"
                    );

                console.log(
                    "Python answered notification result:",
                    pythonResult
                );
            }

            /*
            =================================================
            CALL ENDED
            =================================================
            */

            else if (
                callStatus === "completed" ||
                callStatus === "busy" ||
                callStatus === "no-answer" ||
                callStatus === "failed" ||
                callStatus === "canceled"
            ) {

                console.log("");
                console.log("==========================================");
                console.log("CALL ENDED");
                console.log("==========================================");

                console.log(
                    "Status:",
                    callStatus
                );

                console.log(
                    "Parent:",
                    parentCallSid
                );

                console.log(
                    "Child:",
                    childCallSid
                );

                console.log(
                    "=========================================="
                );

                /*
                ------------------------------------------------
                STOP DETECTION
                ------------------------------------------------
                */

                callInfo.answered =
                    false;

                activeCalls.set(
                    parentCallSid,
                    callInfo
                );

                /*
                ------------------------------------------------
                NOTIFY PYTHON
                ------------------------------------------------
                */

                const pythonResult =
                    await notifyPythonCallStatus(
                        parentCallSid,
                        childCallSid,
                        "ended"
                    );

                console.log(
                    "Python ended notification result:",
                    pythonResult
                );

                /*
                ------------------------------------------------
                REMOVE CALL STATE
                ------------------------------------------------
                */

                activeCalls.delete(
                    parentCallSid
                );

                console.log(
                    "Call state removed:",
                    parentCallSid
                );
            }

            /*
            =================================================
            UNKNOWN STATUS
            =================================================
            */

            else {

                console.log(
                    "Unhandled CallStatus:",
                    callStatus
                );
            }

            /*
            ------------------------------------------------
            ALWAYS RESPOND TO TWILIO
            ------------------------------------------------
            */

            res.sendStatus(200);

        } catch (error) {

            console.error("");
            console.error("==========================================");
            console.error("DIAL STATUS ERROR");
            console.error("==========================================");

            console.error(error);

            console.error(
                "=========================================="
            );

            console.error("");

            /*
            Always return 200 so Twilio does not
            unnecessarily retry the callback.
            */

            res.sendStatus(200);
        }
    }
);

/*
========================================================
NOTIFY PYTHON
========================================================

Node -> Python

POST /call-status

Payload:

{
    call_sid: parentCallSid,
    child_call_sid: childCallSid,
    status: "answered"
}

or

{
    call_sid: parentCallSid,
    child_call_sid: childCallSid,
    status: "ended"
}

========================================================
*/

async function notifyPythonCallStatus(
    parentCallSid,
    childCallSid,
    status
) {

    const payload = {
        call_sid:
            parentCallSid,

        child_call_sid:
            childCallSid,

        status:
            status
    };

    console.log("");
    console.log("------------------------------------------");
    console.log("NOTIFYING PYTHON");
    console.log("------------------------------------------");

    console.log(
        "Python URL:",
        PYTHON_STATUS_URL
    );

    console.log(
        "Payload:",
        payload
    );

    console.log("------------------------------------------");

    /*
    ========================================================
    TRY 3 TIMES
    ========================================================
    */

    for (
        let attempt = 1;
        attempt <= 3;
        attempt++
    ) {

        try {

            console.log(
                `Python notification attempt ${attempt}/3`
            );

            /*
            ------------------------------------------------
            SEND REQUEST
            ------------------------------------------------
            */

            const response =
                await fetch(
                    PYTHON_STATUS_URL,
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body:
                            JSON.stringify(
                                payload
                            )
                    }
                );

            /*
            ------------------------------------------------
            READ RESPONSE
            ------------------------------------------------
            */

            const responseText =
                await response.text();

            console.log(
                "Python HTTP status:",
                response.status
            );

            console.log(
                "Python response:",
                responseText
            );

            /*
            ------------------------------------------------
            SUCCESS
            ------------------------------------------------
            */

            if (response.ok) {

                console.log(
                    "Python notification successful."
                );

                console.log("");

                return {
                    success:
                        true,

                    status:
                        response.status,

                    response:
                        responseText
                };
            }

        } catch (error) {

            console.log(
                "Python notification error:",
                error.message
            );
        }

        /*
        ------------------------------------------------
        WAIT BEFORE RETRY
        ------------------------------------------------
        */

        if (
            attempt < 3
        ) {

            await new Promise(
                resolve =>
                    setTimeout(
                        resolve,
                        500
                    )
            );
        }
    }

    /*
    ========================================================
    ALL ATTEMPTS FAILED
    ========================================================
    */

    console.log(
        "All Python notification attempts failed."
    );

    console.log("");

    return {
        success:
            false
    };
}

/*
========================================================
AI VOICE RISK ALERT
========================================================

Python calls this endpoint when:

1. VAD detects speech
2. Model detects AI voice
3. Risk >= threshold
4. Required consecutive detections reached

Node terminates:

1. Child/agent call
2. Parent/browser call

========================================================
*/

app.post(
    "/twilio/voice-risk-alert",
    async (req, res) => {

        try {

            const {
                call_sid,
                child_call_sid,
                risk_score_pct,
                risk_level,
                prediction,
                track
            } = req.body;

            console.log("");
            console.log("==========================================");
            console.log("VOICE RISK ALERT");
            console.log("==========================================");

            console.log(
                "Parent Call SID:",
                call_sid
            );

            console.log(
                "Child Call SID:",
                child_call_sid
            );

            console.log(
                "Track:",
                track
            );

            console.log(
                "Prediction:",
                prediction
            );

            console.log(
                "Risk:",
                risk_score_pct + "%"
            );

            console.log(
                "Level:",
                risk_level
            );

            console.log(
                "=========================================="
            );

            console.log("");

            /*
            ------------------------------------------------
            VALIDATE PARENT CALL SID
            ------------------------------------------------
            */

            if (!call_sid) {

                return res.status(400).json({
                    success: false,
                    error:
                        "call_sid is required"
                });
            }

            /*
            ------------------------------------------------
            FIND CALL
            ------------------------------------------------
            */

            const callInfo =
                activeCalls.get(
                    call_sid
                );

            /*
            ------------------------------------------------
            GET CHILD SID
            ------------------------------------------------
            */

            let childSid =
                child_call_sid;

            if (
                !childSid &&
                callInfo
            ) {

                childSid =
                    callInfo.childCallSid;
            }

            /*
            ------------------------------------------------
            MARK TERMINATED
            ------------------------------------------------
            */

            if (callInfo) {

                callInfo.terminated =
                    true;

                callInfo.answered =
                    false;

                activeCalls.set(
                    call_sid,
                    callInfo
                );
            }

            /*
            =================================================
            TERMINATE CHILD
            =================================================
            */

            if (childSid) {

                try {

                    console.log(
                        "Terminating child call..."
                    );

                    await client
                        .calls(childSid)
                        .update({
                            status:
                                "completed"
                        });

                    console.log(
                        "Child call terminated:",
                        childSid
                    );

                } catch (error) {

                    console.log(
                        "Child call termination failed:",
                        error.message
                    );
                }

            } else {

                console.log(
                    "No child Call SID available."
                );
            }

            /*
            =================================================
            TERMINATE PARENT
            =================================================
            */

            try {

                console.log(
                    "Terminating parent call..."
                );

                await client
                    .calls(call_sid)
                    .update({
                        status:
                            "completed"
                    });

                console.log(
                    "Parent call terminated:",
                    call_sid
                );

            } catch (error) {

                console.log(
                    "Parent call termination failed:",
                    error.message
                );
            }

            /*
            ------------------------------------------------
            REMOVE ACTIVE CALL
            ------------------------------------------------
            */

            activeCalls.delete(
                call_sid
            );

            console.log("");
            console.log(
                "Call termination process completed."
            );
            console.log("");

            res.json({
                success:
                    true,

                call_terminated:
                    true
            });

        } catch (error) {

            console.error("");
            console.error("==========================================");
            console.error(
                "VOICE RISK ALERT ERROR"
            );
            console.error("==========================================");

            console.error(error);

            console.error(
                "=========================================="
            );

            console.error("");

            res.status(500).json({

                success:
                    false,

                call_terminated:
                    false,

                error:
                    error.message
            });
        }
    }
);

/*
========================================================
TWILIO ACCESS TOKEN
========================================================

A fresh token is generated every time /token
is called.

TTL:
3600 seconds = 1 hour

========================================================
*/

app.get(
    "/token",
    (req, res) => {

        try {

            const identity =
                "browser_caller";

            const AccessToken =
                twilio.jwt.AccessToken;

            const VoiceGrant =
                AccessToken.VoiceGrant;

            /*
            ------------------------------------------------
            VALIDATE CONFIGURATION
            ------------------------------------------------
            */

            if (!TWILIO_ACCOUNT_SID) {
                throw new Error(
                    "TWILIO_ACCOUNT_SID is missing"
                );
            }

            if (!TWILIO_API_KEY) {
                throw new Error(
                    "TWILIO_API_KEY is missing"
                );
            }

            if (!TWILIO_API_SECRET) {
                throw new Error(
                    "TWILIO_API_SECRET is missing"
                );
            }

            if (!TWIML_APP_SID) {
                throw new Error(
                    "TWIML_APP_SID is missing"
                );
            }

            /*
            ------------------------------------------------
            CREATE ACCESS TOKEN
            ------------------------------------------------
            */

            const accessToken =
                new AccessToken(
                    TWILIO_ACCOUNT_SID,
                    TWILIO_API_KEY,
                    TWILIO_API_SECRET,
                    {
                        identity:
                            identity,

                        ttl:
                            3600
                    }
                );

            /*
            ------------------------------------------------
            VOICE GRANT
            ------------------------------------------------
            */

            const voiceGrant =
                new VoiceGrant({
                    outgoingApplicationSid:
                        TWIML_APP_SID,

                    incomingAllow:
                        false
                });

            /*
            ------------------------------------------------
            ADD GRANT
            ------------------------------------------------
            */

            accessToken.addGrant(
                voiceGrant
            );

            /*
            ------------------------------------------------
            GENERATE JWT
            ------------------------------------------------
            */

            const token =
                accessToken.toJwt();

            console.log("");
            console.log("==========================================");
            console.log(
                "NEW TWILIO ACCESS TOKEN GENERATED"
            );
            console.log("==========================================");

            console.log(
                "Identity:",
                identity
            );

            console.log(
                "TTL:",
                "3600 seconds"
            );

            console.log(
                "Expires:",
                "1 hour from now"
            );

            console.log(
                "=========================================="
            );

            console.log("");

            res.json({
                token:
                    token,

                identity:
                    identity
            });

        } catch (error) {

            console.error("");
            console.error("==========================================");
            console.error(
                "TOKEN GENERATION ERROR"
            );
            console.error("==========================================");

            console.error(error);

            console.error(
                "=========================================="
            );

            console.error("");

            res.status(500).json({

                error:
                    "Could not generate Twilio token",

                message:
                    error.message
            });
        }
    }
);

/*
========================================================
PUBLIC NODE URL
========================================================
*/

function getPublicNodeUrl() {

    const url =
        NODE_PUBLIC_URL;

    console.log(
        "NODE_PUBLIC_URL:",
        url
    );

    if (!url) {

        throw new Error(
            "NODE_PUBLIC_URL is missing from .env"
        );
    }

    return url.replace(
        /\/$/,
        ""
    );
}

/*
========================================================
START SERVER
========================================================
*/

app.listen(
    PORT,
    () => {

        console.log("");
        console.log("==========================================");
        console.log(
            "NODE / TWILIO SERVER STARTED"
        );
        console.log("==========================================");

        console.log(
            "Port:",
            PORT
        );

        console.log(
            "Media Stream:",
            MEDIA_STREAM_WS_URL
        );

        console.log(
            "Node Public URL:",
            NODE_PUBLIC_URL
        );

        console.log(
            "Python Status URL:",
            PYTHON_STATUS_URL
        );

        console.log(
            "Agent Number:",
            AGENT_PHONE_NUMBER
        );

        console.log(
            "Twilio API Key:",
            TWILIO_API_KEY
                ? "Configured"
                : "MISSING"
        );

        console.log(
            "Twilio API Secret:",
            TWILIO_API_SECRET
                ? "Configured"
                : "MISSING"
        );

        console.log(
            "Twilio TwiML App:",
            TWIML_APP_SID
                ? "Configured"
                : "MISSING"
        );

        console.log("");

        console.log(
            "Detection starts ONLY after agent answers."
        );

        console.log(
            "Twilio <Say> has been removed."
        );

        console.log(
            "Media Stream track: both_tracks"
        );

        console.log(
            "Dial status callback: <Number>"
        );

        console.log(
            "in-progress = answered"
        );

        console.log(
            "=========================================="
        );

        console.log("");
    }
);
