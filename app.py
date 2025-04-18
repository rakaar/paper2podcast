import os
from flask import Flask, request, render_template, redirect, url_for
from werkzeug.utils import secure_filename
import fitz  # PyMuPDF
import json
import google.generativeai as genai

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Gemini API settings (replace with your actual API key)
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
genai.configure(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-2.5-pro-exp-03-25"

import uuid

import re
import wave

def split_text_into_chunks(text, max_chars=450):
    # Split text into sentences
    sentences = re.split(r'(?<=[.!?]) +', text)
    chunks = []
    current = ''
    for sent in sentences:
        if len(current) + len(sent) + 1 > max_chars:
            if current:
                chunks.append(current.strip())
            current = sent
        else:
            if current:
                current += ' '
            current += sent
    if current:
        chunks.append(current.strip())
    return chunks


@app.route('/', methods=['GET'])
def index():
    return render_template('singlepage.html')

@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'pdf' not in request.files:
        return {'error': 'No file part'}, 400
    file = request.files['pdf']
    if file.filename == '':
        return {'error': 'No selected file'}, 400
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    # Extract text
    text = extract_text_from_pdf(filepath)
    # Print first and last 10 lines (already done in extract_text_from_pdf)
    # Store in temp file for Gemini/question flow
    temp_id = str(uuid.uuid4())
    temp_txt_filename = f"extracted_{temp_id}.txt"
    temp_txt_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_txt_filename)
    with open(temp_txt_path, 'w', encoding='utf-8') as tf:
        tf.write(text)
    return {'temp_txt_filename': temp_txt_filename}

@app.route('/api/questions', methods=['POST'])
def api_questions():
    temp_txt_filename = request.json.get('temp_txt_filename')
    if not temp_txt_filename:
        return {'error': 'No temp filename'}, 400
    temp_txt_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_txt_filename)
    try:
        with open(temp_txt_path, 'r', encoding='utf-8') as tf:
            extracted_text = tf.read()
    except Exception as e:
        return {'error': f'Could not read extracted text: {e}'}, 500
    gemini_response = get_questions_from_gemini(extracted_text)
    # Clean Gemini response: remove markdown code block formatting
    import re
    cleaned = gemini_response.strip()
    # Try to extract JSON block from Markdown code block
    codeblock_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", cleaned)
    if codeblock_match:
        cleaned = codeblock_match.group(1).strip()
    # Fallback: try to find the first JSON object in the text
    else:
        json_match = re.search(r'\{[\s\S]+\}', cleaned)
        if json_match:
            cleaned = json_match.group(0)
    try:
        questions_data = json.loads(cleaned)
        questions = questions_data.get('questions', [])
    except Exception as e:
        print(f"[ERROR] Failed to parse Gemini questions JSON.\nRaw response: {gemini_response}\nCleaned: {cleaned}\nError: {e}")
        return {'error': f'Error parsing questions: {e}', 'raw': gemini_response}, 500
    return {'questions': questions}

@app.route('/api/submit', methods=['POST'])
def api_submit():
    # Print questions and answers in Q/A format to terminal
    data = request.get_json()
    answers = data.get('answers', {})
    questions = data.get('questions', [])
    temp_txt_filename = data.get('temp_txt_filename')
    print("\n==== User Q&A Session ====")
    qa_lines = []
    if questions:
        for idx, q in enumerate(questions):
            qtext = q.get('q', f'Question {idx+1}')
            ans = answers.get(f'q_{idx}', '[No answer]')
            print(f"Q: {qtext}\nA: {ans}\n")
            qa_lines.append(f"Q: {qtext}\nA: {ans}")
    else:
        for k, v in answers.items():
            print(f"{k}: {v}")
            qa_lines.append(f"{k}: {v}")
    print("========================\n")
    # Generate podcast after Q&A
    podcast_text = None
    audio_url = None
    transcript = None
    if temp_txt_filename:
        podcast_text = generate_podcast_from_gemini(temp_txt_filename, qa_lines)
        print("\n===== Generated Podcast Script =====\n")
        print(podcast_text)
        print("\n===================================\n")
        transcript = podcast_text
        # Call Sarvam TTS in chunks
        audio_dir = 'static'
        if not os.path.exists(audio_dir):
            os.makedirs(audio_dir)
        chunked_texts = split_text_into_chunks(podcast_text, max_chars=450)
        audio_chunk_paths = []
        for idx, chunk in enumerate(chunked_texts):
            chunk_path = os.path.join(audio_dir, f'podcast_part_{idx+1}.wav')
            sarvam_success = text_to_speech_sarvam(
                api_key=os.environ.get('SARVAM_API_KEY'),
                text_input=chunk,
                language_code='en-IN',
                speaker_name='meera',
                output_filename=chunk_path
            )
            if sarvam_success:
                audio_chunk_paths.append(chunk_path)
        # Concatenate all .wav files
        final_audio_path = os.path.join(audio_dir, 'podcast_latest.wav')
        if audio_chunk_paths:
            with wave.open(final_audio_path, 'wb') as outfile:
                # Use params from first file
                with wave.open(audio_chunk_paths[0], 'rb') as infile:
                    outfile.setparams(infile.getparams())
                    outfile.writeframes(infile.readframes(infile.getnframes()))
                for chunk_path in audio_chunk_paths[1:]:
                    with wave.open(chunk_path, 'rb') as infile:
                        outfile.writeframes(infile.readframes(infile.getnframes()))
            audio_url = '/static/podcast_latest.wav'
        else:
            audio_url = None
    return {'success': True, 'audio_url': audio_url, 'transcript': transcript}

import requests
import base64

def text_to_speech_sarvam(api_key, text_input, language_code, speaker_name, output_filename="output_audio.wav"):
    """
    Converts text to speech using Sarvam AI TTS API and saves the audio.
    """
    api_url = "https://api.sarvam.ai/text-to-speech"
    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": api_key
    }
    payload = {
        "inputs": [text_input],
        "target_language_code": language_code,
        "speaker": speaker_name,
        "model": "bulbul:v1",
        "pitch": 0,
        "pace": 1.0,
        "loudness": 1.0,
        "speech_sample_rate": 16000,
        "enable_preprocessing": False
    }
    try:
        response = requests.post(api_url, headers=headers, json=payload)
        response.raise_for_status()
        response_data = response.json()
        if "audios" in response_data and response_data["audios"]:
            audio_base64 = response_data["audios"][0]
            audio_bytes = base64.b64decode(audio_base64)
            with open(output_filename, "wb") as audio_file:
                audio_file.write(audio_bytes)
            print(f"Audio successfully saved to {output_filename}")
            return True
        else:
            print("Error: 'audios' key not found or empty in response.")
            print("Response:", response_data)
            return False
    except Exception as e:
        print(f"[Sarvam TTS] API Request failed: {e}")
        return False

def generate_podcast_from_gemini(temp_txt_filename, qa_lines):
    # Read extracted paper content
    upload_folder = app.config['UPLOAD_FOLDER']
    temp_txt_path = os.path.join(upload_folder, temp_txt_filename)
    try:
        with open(temp_txt_path, 'r', encoding='utf-8') as tf:
            paper_content = tf.read()
    except Exception as e:
        print(f"[ERROR] Could not read extracted text: {e}")
        return None
    prompt = f"""
You are an expert science communicator and podcast creator.

Your task: Generate a technical podcast script (length: less than 5 minutes) tailored to the user's background and needs, based on the provided journal paper.

===== Paper Content =====
{paper_content}

===== User Background Q & A =====
{chr(10).join(qa_lines)}

===== Instructions =====
- This is a single-person podcast (monologue).
- Use the user's background to decide the level of technical detail and which concepts to explain.
- The podcast should be engaging, accurate, and accessible for the user.
- Focus on the main findings, methods, and significance of the paper.
- Do not exceed 5 minutes in length if spoken aloud.
- Format the output as a podcast script ready to be read or recorded.
- Please format the response as plain speech only, without any stage directions, sound cues, or labels like "Host" or "Outro Music," as the content will be directly fed into a text-to-speech (TTS) system
"""
    model = genai.GenerativeModel(MODEL_ID)
    response = model.generate_content(prompt)
    return response.text


import sys

def extract_text_from_pdf(filepath):
    try:
        doc = fitz.open(filepath)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        if not text.strip():
            print("[PDF Extraction Warning]: No text could be extracted from the PDF.")
            sys.stdout.flush()
            return ''
        lines = text.splitlines()
        first_10 = '\n'.join(lines[:10])
        last_10 = '\n'.join(lines[-10:]) if len(lines) > 10 else ''
        print("[Extracted PDF Text - First 10 lines]:\n", first_10)
        print("[Extracted PDF Text - Last 10 lines]:\n", last_10)
        print("[Extracted PDF Text]:\n", text[:1000], "...\n[truncated]")
        sys.stdout.flush()
        return text
    except Exception as e:
        import traceback
        print(f"[PDF Extraction Error]: {e}")
        traceback.print_exc()
        sys.stdout.flush()
        return ''

def get_questions_from_gemini(pdf_text):
    # Prompt Gemini for questions
    request_text = """
You need to generate a podcast which is technical in nature with the content of the paper given above. 
But before generating the podcast, generate 5 questions to know the background of the user. The questions should help
you tailor the technical details of the podcast. Most of the questions should have 3 options:
- 🧠 Yep, I know this stuff
- 🤔 Kinda… sounds familiar
- 🤷 Nope, no clue

Return the questions and options in a JSON format like this:

{
    "questions": [
        {
            "q": "What is a convolutional neural network?",
            "options": ["🧠 Yep, I know this stuff", "🤔 Kinda… sounds familiar", "🤷 Nope, no clue"]
        },
        {
            "q": "What is your background?",
            "options": ["Neuroscience expert", "Beginner", "Outside of neuroscience"]
        }
    ]
}

Paper text:
"""
    request_text += pdf_text
    request_text += "\nQuestions:"
    
    model = genai.GenerativeModel(MODEL_ID)
    response = model.generate_content(request_text)

    print('############################################################')
    print("[Gemini API Raw Response]", response.text)
    print('############################################################')

    # Show the raw response on the web page for debug
    return response.text


if __name__ == '__main__':
    app.run(debug=True)
