import os
from flask import Flask, request, render_template, redirect, url_for
from werkzeug.utils import secure_filename
from pdfminer.high_level import extract_text as pdfminer_extract_text
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
    # Store in temp file
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
    cleaned = gemini_response.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.lstrip('`').lstrip('json').lstrip('\n').strip()
    if cleaned.endswith('```'):
        cleaned = cleaned[:cleaned.rfind('```')].strip()
    try:
        questions_data = json.loads(cleaned)
        questions = questions_data.get('questions', [])
    except Exception as e:
        return {'error': f'Error parsing questions: {e}', 'raw': gemini_response}, 500
    return {'questions': questions}

@app.route('/api/submit', methods=['POST'])
def api_submit():
    # Print questions and answers in Q/A format to terminal
    data = request.get_json()
    answers = data.get('answers', {})
    # To get questions, we need to reconstruct them from the last request
    # Instead, let the client send the questions array as well (or store in session, but stateless is better)
    questions = data.get('questions', [])
    print("\n==== User Q&A Session ====")
    if questions:
        for idx, q in enumerate(questions):
            qtext = q.get('q', f'Question {idx+1}')
            ans = answers.get(f'q_{idx}', '[No answer]')
            print(f"Q: {qtext}\nA: {ans}\n")
    else:
        for k, v in answers.items():
            print(f"{k}: {v}")
    print("========================\n")
    return {'success': True}


def extract_text_from_pdf(filepath):
    try:
        text = pdfminer_extract_text(filepath)
        print("[Extracted PDF Text]:\n", text[:1000], "...\n[truncated]")
        return text
    except Exception as e:
        print(f"[PDF Extraction Error]: {e}")
        return ''

@app.route('/questions', methods=['GET', 'POST'])
def ask_questions():
    if request.method == 'POST':
        # Store user answers
        answers = {}
        for key, value in request.form.items():
            if key.startswith('q_'):
                answers[key] = value
        print('User Answers:', answers)
        # Get questions from hidden field
        import ast
        questions_raw = request.form.get('questions_json', '')
        try:
            questions = ast.literal_eval(questions_raw)
        except Exception:
            questions = None
        return render_template('thanks.html', answers=answers, questions=questions)
    # On GET, get temp filename
    temp_txt_filename = request.args.get('temp_txt_filename')
    if not temp_txt_filename:
        return redirect(url_for('upload_pdf'))
    temp_txt_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_txt_filename)
    try:
        with open(temp_txt_path, 'r', encoding='utf-8') as tf:
            extracted_text = tf.read()
    except Exception as e:
        return f'Could not read extracted text: {e}', 500
    # Call Gemini
    gemini_response = get_questions_from_gemini(extracted_text)
    # Clean Gemini response: remove markdown code block formatting
    cleaned = gemini_response.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.lstrip('`').lstrip('json').lstrip('\n').strip()
    if cleaned.endswith('```'):
        cleaned = cleaned[:cleaned.rfind('```')].strip()
    try:
        questions_data = json.loads(cleaned)
        questions = questions_data.get('questions', [])
        parse_error = None
    except Exception as e:
        questions = None
        parse_error = f"Error parsing questions: {e}"
    return render_template('questions.html', questions=questions, extracted_text=extracted_text, gemini_response=gemini_response, parse_error=parse_error)

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



@app.route('/continue', methods=['POST'])
def continue_to_questions():
    # Pass temp filename to questions page via GET
    temp_txt_filename = request.form.get('temp_txt_filename')
    if not temp_txt_filename:
        return redirect(url_for('upload_pdf'))
    return redirect(url_for('ask_questions', temp_txt_filename=temp_txt_filename))


if __name__ == '__main__':
    app.run(debug=True)
