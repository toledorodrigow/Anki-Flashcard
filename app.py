import requests
import time
from datetime import datetime
import os
from dotenv import load_dotenv
import re
import base64
from flask import Flask, send_from_directory, jsonify
from flask_socketio import SocketIO
import threading
from collections import defaultdict

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN_github")
REPO_OWNER = os.getenv("GITHUB_USERNAME")
REPO_NAME = "Anki-Flashcard"
BRANCH = "main"
IMAGE_DIR = "English/images"
ANKICONNECT_URL = "http://localhost:8765"

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", engineio_logger=True)  # Enable logging

questions = []
active_question = None
scores = defaultdict(int)
question_counter = 0
question_timeout = 60

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

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

def anki_connect_request(action, **params):
    return {"action": action, "version": 6, "params": params}

def get_current_card():
    try:
        response = requests.post(
            ANKICONNECT_URL, json=anki_connect_request("guiCurrentCard"), timeout=2
        )
        return response.json().get("result")
    except Exception as e:
        print(f"AnkiConnect error: {e}")
        return None

def process_card(card):
    fields = card["fields"]
    word = re.sub(r"<.*?>", "", fields["Word"]["value"]).strip()
    definition = re.sub(r"<.*?>", "", fields["Definition"]["value"]).strip()
    example = re.sub(r"<.*?>", "", fields["Example"]["value"]).strip()
    image_html = fields["Image"]["value"]
    image_filename = re.search(r'src="(.*?)"', image_html).group(1) if image_html else None
    image_path = os.path.join(
        os.path.expanduser("~"),
        "AppData", "Roaming", "Anki2", "User 1",
        "collection.media", image_filename
    ) if image_filename else None
    return word, definition, example, image_path

def upload_image_to_repo(image_data, filename):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{IMAGE_DIR}/{filename}"
    image_b64 = base64.b64encode(image_data).decode("utf-8")
    data = {"message": f"Add image {filename}", "content": image_b64, "branch": BRANCH}
    return requests.put(url, headers=headers, json=data).status_code in (200, 201)

def update_markdown_file(new_entry, md_file):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{md_file}"
    response = requests.get(url, headers=headers)
    sha = None
    current_content = ""
    if response.status_code == 200:
        file_data = response.json()
        sha = file_data["sha"]
        current_content = base64.b64decode(file_data["content"]).decode("utf-8")
    new_content = current_content + new_entry
    new_content_b64 = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
    data = {"message": "Add new flashcard entry", "content": new_content_b64, "branch": BRANCH, "sha": sha}
    return requests.put(url, headers=headers, json=data).status_code == 200

def upload_card_data(card_data):
    global active_question, question_counter
    question_counter += 1
    word, definition, example, image_path, timestamp = card_data
    md_file = f"English/{timestamp.year}/{timestamp.month:02d}/Flashcards_{timestamp.date()}.md"
    sanitized_word = re.sub(r"[^a-zA-Z0-9]", "_", word)
    image_filename = f"{sanitized_word}_{timestamp.strftime('%Y%m%d%H%M%S')}.jpg"
    image_url = None

    if image_path and os.path.exists(image_path):
        try:
            with open(image_path, "rb") as img_file:
                if upload_image_to_repo(img_file.read(), image_filename):
                    image_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{IMAGE_DIR}/{image_filename}"
        except Exception as e:
            print(f"Image upload error: {e}")

    replacement = '*' * len(word)
    pattern = re.escape(word)
    modified_definition = re.sub(pattern, replacement, definition)
    modified_example = re.sub(pattern, replacement, example)

    formatted_time = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    markdown_entry = f"""## {formatted_time} - {word}
**Definition**: {modified_definition}

{f'![Image]({image_url})' if image_url else ''}

**Example**: {modified_example}
---
"""
    update_markdown_file(markdown_entry, md_file)

    question_id = f"{question_counter}_{int(time.time())}"
    new_question = {
        'id': question_id,
        'number': question_counter,
        'word': word,
        'definition': modified_definition,
        'example': modified_example,
        'image': image_url,
        'correct_answer': re.sub(r"[^a-zA-Z0-9]", "", word).lower(),
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
        'id': question_id,
        'number': question_counter,
        'question': modified_definition,
        'image': image_url,
        'example': modified_example,
        'end_time': new_question['end_time'],
        'timeout': question_timeout
    })

    threading.Thread(target=question_timer, args=(question_id,)).start()
    return True

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

def start_server():
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)

def main_loop():
    last_card_id = None
    while True:
        try:
            current_card = get_current_card()
            if current_card and current_card.get("cardId") != last_card_id:
                try:
                    card_data = (*process_card(current_card), datetime.now())
                    upload_card_data(card_data)
                    last_card_id = current_card["cardId"]
                except Exception as e:
                    print(f"Error processing card: {e}")
        except Exception as e:
            print(f"Error: {e}")
def fetch_existing_questions():
    try:
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/English"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            # Process directory structure to find all markdown files
            # Add logic to parse historical questions from existing markdown files
            # Populate the 'questions' list with historical data
            print("Fetched existing questions from GitHub")
    except Exception as e:
        print(f"Error fetching existing questions: {e}")
def schedule_content_refresh():
    def refresh_job():
        while True:
            time.sleep(300)  # 5 minutes
            fetch_existing_questions()
            socketio.emit('content_refresh')
    
    threading.Thread(target=refresh_job, daemon=True).start()

# Start this after server initialization
schedule_content_refresh()
# Add this before starting the server
fetch_existing_questions()
if __name__ == "__main__":
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    main_loop()
