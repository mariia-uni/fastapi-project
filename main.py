from fastapi import FastAPI, status, Request
from youtube_transcript_api import YouTubeTranscriptApi
from groq import Groq
from dotenv import dotenv_values, load_dotenv
import os
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from groq import APIStatusError
import sqlite3
import json

load_dotenv()

config = dotenv_values(".env")

app = FastAPI()
ytt_api = YouTubeTranscriptApi()
client = Groq(
    api_key = os.getenv('OPENAI_API_KEY'),
)
connection = sqlite3.connect('videos.db', check_same_thread=False)

cursor = connection.cursor()

templates = Jinja2Templates(directory="templates")

video_id = ""
summary = ""

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "home.html",
        status_code=status.HTTP_200_OK,
    )

@app.get("/summary", response_class=HTMLResponse)
def get_summary(request: Request, video_url: str):
    try:
            video_id = video_url.replace("https://www.youtube.com/watch?v=", "")
            transcript = []
            fetched_text = ytt_api.fetch(video_id, languages=['en', 'uk'])
            for snippet in fetched_text:
                transcript.append(snippet.text)
            transcript = " ".join(transcript)
            cursor.execute("create table if not exists videos (video_id TEXT PRIMARY KEY, transcript TEXT, summary TEXT, cards TEXT)")
            cursor.execute("select * from videos where video_id=:video_id", {"video_id": video_id})
            if not cursor.fetchone():
                chat_completion = client.chat.completions.create(
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a helpful assistent.",
                        },
                        {
                            "role": "user",
                            "content": f"Write a 100 word summary of this video transcript: {transcript}.",
                        },
                    ],
                    model="llama-3.1-8b-instant",
                )
                summary = chat_completion.choices[0].message.content
                cursor.execute("insert into videos values (?, ?, ?, ?)", (video_id, transcript, summary, None))
                connection.commit()
            else:
                cursor.execute("select * from videos where video_id=:video_id", {"video_id": video_id})
                summary = cursor.fetchone()[2]
            return templates.TemplateResponse(
                request,
                "summary.html",
                {"video_url": video_url, "video_id": video_id, "transcript": transcript, "summary": summary},
            )
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not retrieve a transcript for this video."
        )
    except (APIStatusError):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Request too large for a model."
        )

@app.exception_handler(HTTPException)
def general_http_exception_handler(request: Request, exception: HTTPException):
    message = (
        exception.detail
        if exception.detail
        else "An error occured. Please, try again."
    )

    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": exception.status_code,
            "message": message,
        },
        status_code=exception.status_code,
    )

@app.get("/cards")
def get_cards(request: Request, video_id: str):
    cursor.execute("select * from videos where video_id=:video_id", {"video_id": video_id})
    if not cursor.fetchone()[3]:
        cursor.execute("select transcript from videos where video_id=:video_id", {"video_id": video_id})
        transcript = cursor.fetchone()
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert Anki flashcard generator.",
                },
                {
                    "role": "user",
                    "content": f"""Analyze the video transcript provided below and extract exactly 10 high-quality flashcards to help me learn the material.
                    
                    Strict Output Rules:
                    1. Provide the final output ONLY as a raw Python list of dictionaries (valid JSON array format). Do not include any introductory text, explanatory prose, code fences (such as ```python or 
                    ```json), markdown formatting, or bullet points. Output only the data structure.
                    2. The quantity of items in the list must be exactly 10.
                    3. Every dictionary in the list must follow this precise structure:
                    {{"question": "The question string here", "answer": "The answer string here"}}

                    Transcript to analyze:
                    {transcript}""",
                },
            ],
            model="llama-3.1-8b-instant",
        )
        cards = json.loads(chat_completion.choices[0].message.content)
    else:
        cursor.execute("select * from videos where video_id=:video_id", {"video_id": video_id})
        cards = cursor.fetchone()[3]
    return templates.TemplateResponse(
        request,
        "cards.html",
        {
            "cards": cards,
        },
        status_code=status.HTTP_200_OK,
    )