import os
from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO
from collections import defaultdict
from dotenv import load_dotenv
import time
import re
load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
app.config['SECRET_KEY'] = os.getenv('SYNC_SECRET', 'secret!')

questions = []
active_question = None
scores = defaultdict(int)
question_counter = 0
question_timeout = 60

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

@app.route('/api/new_question', methods=['POST'])
def receive_new_question():
    global question_counter, active_question
    data = request.json
    
    question_counter += 1
    question_id = f"{question_counter}_{int(time.time())}"
    
    new_question = {
        'id': question_id,
        'number': question_counter,
        'word': data['word'],
        'definition': data['definition'],
        'example': data['example'],
        'image': data['image'],
        'correct_answer': data['correct_answer'],
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
        'question': data['definition'],
        'image': data['image'],
        'example': data['example'],
        'end_time': new_question['end_time'],
        'timeout': question_timeout
    })

    return jsonify(success=True)

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

if __name__ == '__main__':
    socketio.run(app)
