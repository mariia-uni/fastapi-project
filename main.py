from fastapi import FastAPI, status, Request
from youtube_transcript_api import YouTubeTranscriptApi
from groq import Groq
from dotenv import dotenv_values, load_dotenv
import os
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
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
def get_summary(request: Request, video_url: str, format: str = None):
    try:
            video_id = video_url.replace("https://www.youtube.com/watch?v=", "")

            # Ensure table and columns exist
            cursor.execute("create table if not exists videos (video_id TEXT PRIMARY KEY, transcript TEXT, summary TEXT, cards TEXT, tests TEXT, conspect TEXT)")
            cursor.execute("PRAGMA table_info(videos)")
            existing_cols = [r[1] for r in cursor.fetchall()]
            if 'tests' not in existing_cols:
                cursor.execute("ALTER TABLE videos ADD COLUMN tests TEXT")
            if 'conspect' not in existing_cols:
                cursor.execute("ALTER TABLE videos ADD COLUMN conspect TEXT")

            # Check if video already exists in DB
            cursor.execute("select * from videos where video_id=:video_id", {"video_id": video_id})
            video_data = cursor.fetchone()

            # If video exists and user requested a specific format, show existing content if present
            if video_data and format:
                # columns: 0:id,1:transcript,2:summary,3:cards,4:tests,5:conspect
                if format.lower() in ("summary",):
                    if video_data[2]:
                        transcript = video_data[1]
                        summary = video_data[2]
                        return templates.TemplateResponse(request, "summary.html", {"video_url": video_url, "video_id": video_id, "transcript": transcript, "summary": summary})
                if format.lower() in ("conspect", "conspect.html"):
                    if len(video_data) > 5 and video_data[5]:
                        return templates.TemplateResponse(request, "conspect.html", {"conspect": video_data[5]})
                    # else redirect to conspect generator which will create and save it
                    return RedirectResponse(url=f"/conspect?video_id={video_id}")
                if format.lower() in ("cards", "flashcards"):
                    if video_data[3]:
                        try:
                            cards = json.loads(video_data[3])
                        except Exception:
                            cards = None
                        if cards:
                            return templates.TemplateResponse(request, "cards.html", {"cards": cards}, status_code=status.HTTP_200_OK)
                    return RedirectResponse(url=f"/cards?video_id={video_id}")
                if format.lower() in ("tests", "test"):
                    if len(video_data) > 4 and video_data[4]:
                        try:
                            tests = json.loads(video_data[4])
                        except Exception:
                            tests = None
                        if tests:
                            return templates.TemplateResponse(request, "test.html", {"test": tests}, status_code=200)
                    return RedirectResponse(url=f"/test?video_id={video_id}")

            # If video not in DB, fetch transcript and generate summary (existing behavior)
            transcript = []
            fetched_text = ytt_api.fetch(video_id, languages=['en', 'uk'])
            for snippet in fetched_text:
                transcript.append(snippet.text)
            transcript = " ".join(transcript)

            if not video_data:
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
                cursor.execute("insert into videos values (?, ?, ?, ?, ?, ?)", (video_id, transcript, summary, None, None, None))
                connection.commit()
            else:
                # video exists but user didn't request a specific format; return existing summary
                summary = video_data[2]
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
    try:
        cursor.execute("select * from videos where video_id=:video_id", {"video_id": video_id})
        video_data = cursor.fetchone()
        
        if not video_data:
            raise HTTPException(status_code=404, detail="Відео не знайдено.")
            
        if not video_data[3]: # Якщо карток ще немає в базі
            cursor.execute("select transcript from videos where video_id=:video_id", {"video_id": video_id})
            transcript = cursor.fetchone()[0]
            
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
                        2. Do NOT use any backslashes (\) or special escape characters in the text.
                        3. The quantity of items in the list must be exactly 10.
                        4. Every dictionary in the list must follow this precise structure:
                        {{"question": "The question string here", "answer": "The answer string here"}}

                        Transcript to analyze:
                        {transcript}""",
                    },
                ],
                model="llama-3.1-8b-instant",
            )
            
            # Очищаємо текст від можливого маркдауну, який іноді ліпить ШІ
            raw_content = chat_completion.choices[0].message.content
            cleaned_content = raw_content.replace("```json", "").replace("```", "").strip()
            
            try:
                cards = json.loads(cleaned_content)
            except json.JSONDecodeError:
                # Якщо ШІ все одно видав кривий JSON, красиво просимо юзера повторити
                raise HTTPException(
                    status_code=422,
                    detail="Штучний інтелект згенерував текст з неправильним форматуванням. Будь ласка, поверніться назад і натисніть кнопку 'Картки Anki' ще раз."
                )
            
            # Зберігаємо згенеровані картки
            cursor.execute("update videos set cards=:cards where video_id=:video_id", {"cards": json.dumps(cards), "video_id": video_id})
            connection.commit()
        else:
            # Дістаємо збережені картки
            cards = json.loads(video_data[3])
            
        return templates.TemplateResponse(
            request,
            "cards.html",
            {
                "cards": cards,
            },
            status_code=status.HTTP_200_OK,
        )
        
    except HTTPException:
        # Щоб наші красиві помилки 404 та 422 прокидалися в error.html
        raise
    except Exception as e:
        print(f"Помилка: {e}")
        raise HTTPException(
            status_code=500,
            detail="Виникла помилка при генерації (можливо, відео занадто довге). Спробуйте інше відео."
        )
@app.get("/conspect", response_class=HTMLResponse)
def get_conspect(request: Request, video_id: str):
    # Try to fetch saved conspect and transcript from DB
    cursor.execute("select conspect, transcript from videos where video_id=:video_id", {"video_id": video_id})
    result = cursor.fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Транскрипт не знайдено. Спочатку згенеруйте Summary.")

    conspect_saved, transcript = result[0], result[1]

    if conspect_saved:
        return templates.TemplateResponse(
            request,
            "conspect.html",
            {"conspect": conspect_saved},
        )

    # Generate conspect and save to DB
    chat_completion = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": "Ти експерт-викладач. Твоя мета - робити ідеальні конспекти.",
            },
            {
                "role": "user",
                "content": f"""Зроби детальний, гарно структурований конспект цього відео. 
                Використовуй HTML теги для форматування: <h2> для заголовків, <ul> та <li> для списків, <strong> для виділення головного. 
                Не пиши ніякого вступного тексту, видай ТІЛЬКИ HTML код конспекту.
                Транскрипт: {transcript}""",
            },
        ],
        model="llama-3.1-8b-instant",
    )
    conspect_html = chat_completion.choices[0].message.content

    try:
        cursor.execute("update videos set conspect=:conspect where video_id=:video_id", {"conspect": conspect_html, "video_id": video_id})
        connection.commit()
    except Exception as e:
        print(f"Помилка збереження конспекту: {e}")

    return templates.TemplateResponse(
        request,
        "conspect.html",
        {"conspect": conspect_html},
    )

@app.get("/test")
def get_test(request: Request, video_id: str):
    try:
        # Check if we already have tests saved for this video
        cursor.execute("select * from videos where video_id=:video_id", {"video_id": video_id})
        video_data = cursor.fetchone()

        if not video_data:
            return templates.TemplateResponse(request, "error.html", {"status_code": 404, "message": "Транскрипт не знайдено. Спочатку згенеруйте Summary."}, status_code=404)

        # If tests already exist in DB, return them
        if len(video_data) > 4 and video_data[4]:
            try:
                saved_test = json.loads(video_data[4])
            except Exception:
                saved_test = None

            if saved_test:
                return templates.TemplateResponse(
                    request,
                    "test.html",
                    {"test": saved_test},
                    status_code=200,
                )

        # Otherwise generate tests from transcript and save
        transcript = video_data[1]

        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert test generator. Output ONLY valid JSON.",
                },
                {
                    "role": "user",
                    "content": f"""Analyze the transcript and create exactly 5 multiple choice questions.

                    CRITICAL RULES:
                    1. Output ONLY a raw JSON array.
                    2. Do not forget commas between objects.
                    3. Format must be exactly: [{{"question": "Q?", "options": ["A", "B", "C", "D"], "answer": "The exact correct option string"}}]

                    Transcript: {transcript}""",
                },
            ],
            model="llama-3.1-8b-instant",
        )
        raw_content = chat_completion.choices[0].message.content

        # Extract JSON array from model output
        import re
        match = re.search(r'\[.*\]', raw_content, re.DOTALL)
        if match:
            cleaned_content = match.group(0)
        else:
            cleaned_content = raw_content

        try:
            test_data = json.loads(cleaned_content)
        except json.JSONDecodeError:
            return templates.TemplateResponse(request, "error.html", {"status_code": 422, "message": "ШІ припустився помилки у форматуванні тесту. Будь ласка, поверніться назад і спробуйте ще раз."}, status_code=422)

        # Save generated test to DB
        try:
            cursor.execute("update videos set tests=:tests where video_id=:video_id", {"tests": json.dumps(test_data), "video_id": video_id})
            connection.commit()
        except Exception as e:
            print(f"Помилка збереження тесту: {e}")

        return templates.TemplateResponse(
            request,
            "test.html",
            {"test": test_data},
            status_code=200,
        )
    except Exception as e:
        print(f"Помилка генерації тесту: {e}")
        return templates.TemplateResponse(request, "error.html", {"status_code": 500, "message": "Виникла помилка при створенні тесту. Спробуйте інше відео."}, status_code=500)