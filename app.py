import os
from flask import Flask, send_from_directory, jsonify
from flask_socketio import SocketIO
from flask_cors import CORS
from datetime import datetime
import time
from collections import defaultdict
import threading
import requests
import re
import base64
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Configuration
REPO_OWNER = os.getenv("GITHUB_USERNAME")
REPO_NAME = "Anki-Flashcard"
BRANCH = "main"
IMAGE_DIR = "English/images"
SYNC_SECRET = os.getenv("SYNC_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

questions = []
active_question = None
scores = defaultdict(int)
question_counter = 0
question_timeout = 60

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

# Web routes
@app.route('/')
def serve_interface():
    return send_from_directory('public', 'index.html')

@app.route('/questions')
def get_questions():
    return jsonify([{
        'id': q['id'],
        'number': q['number'],
        'question': q['definition'],
        'example': q['example'],
        'image': q['image']
    } for q in reversed(questions)])

@app.route('/scores')
def get_scores():
    return jsonify(sorted(scores.items(), key=lambda x: x[1], reverse=True)[:10])

# API endpoints for client
@app.route('/api/card', methods=['POST'])
def receive_card():
    auth_header = requests.headers.get('Authorization')
    if auth_header != f"Bearer {SYNC_SECRET}":
        return jsonify({"error": "Unauthorized"}), 401
    
    card_data = requests.json
    process_and_broadcast_question(card_data)
    return jsonify({"status": "received"})

def process_and_broadcast_question(card_data):
    global active_question, question_counter
    question_counter += 1
    
    new_question = {
        'id': f"{question_counter}_{int(time.time())}",
        'number': question_counter,
        'word': card_data['word'],
        'definition': card_data['definition'],
        'example': card_data['example'],
        'image': card_data['image'],
        'correct_answer': re.sub(r"[^a-zA-Z0-9]", "", card_data['word']).lower(),
        'end_time': time.time() + question_timeout,
        'answered_users': set(),
        'active': True
    }

    for q in questions:
        q['active'] = False

    questions.append(new_question)
    if len(questions) > 50:
        questions.pop(0)

    active_question = new_question
    socketio.emit('new_question', {
        'id': new_question['id'],
        'number': question_counter,
        'question': new_question['definition'],
        'image': new_question['image'],
        'example': new_question['example'],
        'end_time': new_question['end_time'],
        'timeout': question_timeout
    })

    threading.Thread(target=question_timer, args=(new_question['id'],)).start()

def question_timer(question_id):
    time.sleep(question_timeout)
    socketio.emit('question_end', {'question_id': question_id})

@socketio.on('answer')
def handle_answer(data):
    if 'user' not in data or 'question_id' not in data:
        return
    
    username = data['user'][:15].strip()
    question_id = data['question_id']
    answer = re.sub(r"[^a-zA-Z0-9]", "", data['answer']).lower()
    is_current = data.get('is_current', False)

    question = next((q for q in questions if q['id'] == question_id), None)
    if not question or username in question['answered_users']:
        return

    correct = answer == question['correct_answer']
    scored = False

    if correct and is_current and question['active']:
        scores[username] += 1
        scored = True

    question['answered_users'].add(username)

    socketio.emit('answer_result', {
        'user': username,
        'correct': correct,
        'scored': scored,
        'question_id': question_id
    })

def upload_to_github(image_b64, filename, md_content):
    # Upload image
    image_url = None
    if image_b64:
        image_res = requests.put(
            f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{IMAGE_DIR}/{filename}",
            headers=headers,
            json={
                "message": f"Add image {filename}",
                "content": image_b64,
                "branch": BRANCH
            }
        )
        if image_res.status_code in (200, 201):
            image_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{IMAGE_DIR}/{filename}"

    # Upload markdown
    md_path = f"English/{datetime.now().year}/{datetime.now().month:02d}/Flashcards_{datetime.now().date()}.md"
    md_res = requests.put(
        f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{md_path}",
        headers=headers,
        json={
            "message": "Add new flashcard entry",
            "content": base64.b64encode(md_content.encode("utf-8")).decode("utf-8"),
            "branch": BRANCH
        }
    )
    return image_url if image_res.status_code in (200, 201) else None

if __name__ == '__main__':
    socketio.run(app)
