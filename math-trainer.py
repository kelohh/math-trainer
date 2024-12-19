from flask import Flask, render_template, request, redirect, url_for, jsonify, session, g, make_response
from flask_session import Session
from flask.sessions import SecureCookieSessionInterface, SecureCookieSession
import redis
import random
import os
import json
import threading
from datetime import timedelta
import logging
import time
import uuid

logging.basicConfig(level=logging.ERROR)


class CustomSessionInterface(SecureCookieSessionInterface):
    def __init__(self, redis_connection):
        self.redis = redis_connection
        super().__init__()

    def _generate_sid(self):
        return str(uuid.uuid4())

    def open_session(self, app, request):
        sid = request.cookies.get(app.config['SESSION_COOKIE_NAME'])
        if not sid:
            sid = self._generate_sid()
            session_data = SecureCookieSession()
            session_data.sid = sid
            session_data.new = True
        else:
            val = self.redis.get(app.config['SESSION_KEY_PREFIX'] + sid)
            if val is None:
                session_data = SecureCookieSession()
                session_data.sid = sid
                session_data.new = True
            else:
                data = self.serializer.loads(val)
                session_data = SecureCookieSession(data)
                session_data.sid = sid
                session_data.new = False
        return session_data

    def save_session(self, app, session, response):
        domain = self.get_cookie_domain(app)
        if not session:
            if session.modified:
                self.redis.delete(app.config['SESSION_KEY_PREFIX'] + session.sid)
                response.delete_cookie(app.config['SESSION_COOKIE_NAME'], domain=domain)
            return
        redis_exp = int(app.permanent_session_lifetime.total_seconds())
        val = self.serializer.dumps(dict(session))
        self.redis.setex(app.config['SESSION_KEY_PREFIX'] + session.sid, redis_exp, val)
        response.set_cookie(app.config['SESSION_COOKIE_NAME'], session.sid, max_age=redis_exp, httponly=True,
                            domain=domain)


app = Flask(__name__)

# Configure Redis session storage
app.config["SESSION_TYPE"] = "redis"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
app.config["SESSION_KEY_PREFIX"] = "session:"
app.config["SESSION_REDIS"] = redis.from_url("redis://redis:6379")
app.config["SESSION_COOKIE_NAME"] = "session"

# Initialize the session
server_session = Session(app)

# Use custom session interface
app.session_interface = CustomSessionInterface(app.config["SESSION_REDIS"])

app.secret_key = 'your_secret_key'
app.permanent_session_lifetime = timedelta(days=365)

# Default configuration
default_config = {
    "allow_negative_answers": False,
    "min_number": 1,
    "max_number": 100,
    "timer_enabled": True,
    "timer_seconds": 60,
    "show_reset_button": True,
    "max_result": 100,
    "addition": True,
    "subtraction": True,
    "multiplication": True,
    "division": True,
    "integer_results_only": True,
    "challenge_mode": True,
    "challenge_problems": 10
}

SCOREBOARD_FILE_PATH = "static_data/scoreboard.json"

# App state
scoreboard_data = []


def load_scoreboard():
    global scoreboard_data
    if os.path.exists(SCOREBOARD_FILE_PATH):
        with open(SCOREBOARD_FILE_PATH, "r") as scoreboard_file:
            scoreboard_data = json.load(scoreboard_file)
    else:
        scoreboard_data = []
        save_scoreboard()
    logging.debug(f"Scoreboard loaded: {scoreboard_data}")


def save_scoreboard():
    global scoreboard_data
    try:
        with open(SCOREBOARD_FILE_PATH, "w") as scoreboard_file:
            json.dump(scoreboard_data, scoreboard_file, indent=4)
        logging.debug("Scoreboard saved successfully.")
    except Exception as e:
        logging.error(f"Error saving scoreboard: {e}")


load_scoreboard()


def generate_problem(config):
    operators = []
    if config.get("addition"):
        operators.append("+")
    if config.get("subtraction"):
        operators.append("-")
    if config.get("multiplication"):
        operators.append("*")
    if config.get("division"):
        operators.append("/")

    while True:
        num1 = random.randint(config["min_number"], config["max_number"])
        num2 = random.randint(config["min_number"], config["max_number"])
        operator = random.choice(operators)

        if operator == "/" and num2 == 0:
            continue

        if not config["allow_negative_answers"] and operator == "-" and num1 < num2:
            num1, num2 = num2, num1

        answer = eval(f"{num1} {operator} {num2}")
        if operator == "/":
            if config["integer_results_only"]:
                if num1 % num2 != 0:
                    continue
                answer = num1 // num2
            else:
                answer = round(answer, 2)

        if answer <= config["max_result"]:
            break

    return {"num1": num1, "num2": num2, "operator": operator, "answer": answer}


def timer_function(sid):
    redis_conn = app.config["SESSION_REDIS"]
    session_key_prefix = app.config["SESSION_KEY_PREFIX"]
    session_serializer = app.session_interface.serializer

    while True:
        time.sleep(1)
        session_data = redis_conn.get(session_key_prefix + sid)
        if session_data is None:
            logging.debug('Session data not found, stopping timer')
            break

        session_data = session_serializer.loads(session_data)
        logging.debug('Timer function running')
        if not session_data.get('timer_active'):
            logging.debug('Timer stopped')
            break
        if session_data.get('timer_seconds_left', 0) > 0:
            session_data['timer_seconds_left'] -= 1
            logging.debug(f'Timer updated: {session_data["timer_seconds_left"]} seconds left')
        else:
            if session_data.get('current_problem'):
                session_data['stats']['bad'] += 1
                session_data['last_answer_correct'] = False
                session_data['last_problem'] = session_data['current_problem']
                session_data['current_problem'] = generate_problem(session_data['config'])
                session_data['timer_seconds_left'] = session_data['config']["timer_seconds"]
                if session_data['config']["challenge_mode"]:
                    session_data['problems_left'] -= 1
                if session_data['config']["challenge_mode"] and session_data['problems_left'] <= 0:
                    session_data['timer_active'] = False
                    session_data['challenge_completed'] = True
                    break

        redis_conn.setex(session_key_prefix + sid, int(app.permanent_session_lifetime.total_seconds()),
                         session_serializer.dumps(session_data))


@app.route("/", methods=["GET", "POST"])
def home():
    if 'config' not in session:
        session['config'] = default_config.copy()
    if 'stats' not in session:
        session['stats'] = {"good": 0, "bad": 0}
    if 'current_problem' not in session:
        session['current_problem'] = None
    if 'last_problem' not in session:
        session['last_problem'] = None
    if 'timer_active' not in session:
        session['timer_active'] = False
    if 'timer_seconds_left' not in session:
        session['timer_seconds_left'] = 0
    if 'last_answer_correct' not in session:
        session['last_answer_correct'] = None
    if 'problems_left' not in session:
        session['problems_left'] = None

    is_correct = None

    if request.method == "POST":
        action = request.form.get("action")
        if action == "start":
            session['current_problem'] = generate_problem(session['config'])
            session['timer_active'] = session['config']["timer_enabled"]
            session['timer_seconds_left'] = session['config'].get("timer_seconds", 60)
            session['last_answer_correct'] = None
            session['stats'] = {"good": 0, "bad": 0}
            if session['config']["challenge_mode"]:
                session['problems_left'] = session['config']["challenge_problems"]
            else:
                session['problems_left'] = None
            session.modified = True
            if session['timer_active']:
                thread = threading.Thread(target=timer_function, args=(session.sid,))
                thread.daemon = True
                thread.start()
        elif action == "stop":
            session['timer_active'] = False
            session['timer_seconds_left'] = 0
            session.modified = True
        elif action == "submit" and session['current_problem']:
            user_answer = request.form.get("answer")
            try:
                user_answer = float(user_answer)
                is_correct = user_answer == session['current_problem']["answer"]
                session['stats']["good" if is_correct else "bad"] += 1
                session['last_answer_correct'] = is_correct
                if is_correct:
                    session['timer_seconds_left'] = session['config'].get("timer_seconds", 60)
            except ValueError:
                is_correct = False
                session['stats']["bad"] += 1
                session['last_answer_correct'] = False

            session['last_problem'] = session['current_problem']
            session['current_problem'] = generate_problem(session['config'])
            if session['timer_active']:
                session['timer_seconds_left'] = session['config'].get("timer_seconds", 60)
            if session['config']["challenge_mode"]:
                session['problems_left'] -= 1
            if session['config']["challenge_mode"] and session['problems_left'] <= 0:
                session['timer_active'] = False
                return redirect(url_for("challenge_completed"))
            session.modified = True

    return render_template(
        "index.html",
        problem=session['current_problem'],
        last_problem=session['last_problem'],
        is_correct=session['last_answer_correct'],
        stats=session['stats'],
        timer_active=session['timer_active'],
        timer_seconds_left=session['timer_seconds_left'],
        config=session['config'],
        problems_left=session['problems_left']
    )


@app.route("/reset", methods=["GET"])
def reset():
    session['stats'] = {"good": 0, "bad": 0}
    session['current_problem'] = None
    session['last_problem'] = None
    session['timer_active'] = False
    session['timer_seconds_left'] = 0
    session['last_answer_correct'] = None
    session['problems_left'] = None
    session.modified = True
    return redirect(url_for("home"))


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if 'config' not in session:
        session['config'] = default_config.copy()

    if request.method == "POST":
        print("Form data received:", request.form)
        updated = False
        config_keys = session['config'].keys()

        for key in config_keys:
            if key in request.form:
                if isinstance(session['config'][key], bool):
                    session['config'][key] = request.form.get(key) == 'on'
                elif isinstance(session['config'][key], int):
                    session['config'][key] = int(request.form.get(key))
                else:
                    session['config'][key] = request.form.get(key)
                updated = True
            else:
                if isinstance(session['config'][key], bool):
                    session['config'][key] = False

        print("Updated config:", session['config'])

        if session['config']["challenge_mode"]:
            session['config']["timer_enabled"] = True

        session.modified = True
        return redirect(url_for("config_page"))

    return render_template("config.html", config=session['config'])


@app.route("/timer", methods=["GET"])
def get_timer():
    return jsonify({
        "timer_seconds_left": session.get('timer_seconds_left', 0),
        "problem": session.get('current_problem'),
        "timer_active": session.get('timer_active', False),
        "stats": session.get('stats', {"good": 0, "bad": 0}),
        "last_problem": session.get('last_problem'),
        "is_correct": session.get('last_answer_correct'),
        "problems_left": session.get('problems_left'),
        "config": session.get('config', default_config)
    })


@app.route("/challenge-completed", methods=["GET", "POST"])
def challenge_completed():
    global scoreboard_data
    if request.method == "POST":
        name = request.form.get("name")
        accuracy = round(session['stats']["good"] / (session['stats']["good"] + session['stats']["bad"]) * 100, 3)
        score = session['stats']["good"] * int(accuracy)
        scoreboard_data.append({"name": name, "score": score})
        scoreboard_data.sort(key=lambda x: x["score"], reverse=True)
        for i, entry in enumerate(scoreboard_data):
            entry["place"] = i + 1
        save_scoreboard()
        logging.debug(f"New scoreboard data: {scoreboard_data}")
        return render_template("scoreboard.html", scoreboard=scoreboard_data)

    return render_template("challenge_completed.html")


@app.route("/scoreboard", methods=["GET"])
def display_scoreboard():
    global scoreboard_data
    logging.debug(f"Displaying scoreboard: {scoreboard_data}")
    return render_template("scoreboard.html", scoreboard=scoreboard_data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)