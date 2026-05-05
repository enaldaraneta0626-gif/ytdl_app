from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import os
import threading
import uuid
import requests as http_requests
from pathlib import Path
from datetime import datetime, date
import tempfile

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///ytdl.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+pg8000://', 1)
if database_url.startswith('postgresql://') and 'pg8000' not in database_url:
    database_url = database_url.replace('postgresql://', 'postgresql+pg8000://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ✏️ CHANGE YOUR AD VIDEO URL HERE ANYTIME
AD_YOUTUBE_URL = "https://www.youtube.com/watch?v=7tZAhO7BEnA"

GUEST_DAILY_LIMIT = 5
FREE_USER_DAILY_LIMIT = 10
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')

# Cobalt API instances (fallbacks)
COBALT_INSTANCES = [
    "https://cobalt.imput.net",
    "https://api.cobalt.tools",
    "https://cobalt.api.timelessnesses.me",
    "https://cobalt.urdushayari.cf",
    "https://cobalt.synzr.space",
    "https://co.wuk.sh",
    "https://cobalt.riversiderocksalt.me",
    "https://cobalt.drgns.space",
]

jobs = {}

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_premium = db.Column(db.Boolean, default=False)
    daily_download_count = db.Column(db.Integer, default=0)
    last_download_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    downloads = db.relationship('Download', backref='user', lazy=True)

    def reset_if_new_day(self):
        if self.last_download_date != date.today():
            self.daily_download_count = 0
            self.last_download_date = date.today()

class Download(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    title = db.Column(db.String(300))
    mode = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def get_guest_downloads_today():
    if session.get('guest_dl_date') != str(date.today()):
        session['guest_dl_date'] = str(date.today())
        session['guest_downloads'] = 0
    return session.get('guest_downloads', 0)

def can_download():
    if current_user.is_authenticated:
        if current_user.is_premium:
            return True, None
        current_user.reset_if_new_day()
        if current_user.daily_download_count >= FREE_USER_DAILY_LIMIT:
            return False, 'limit'
        return True, None
    else:
        if get_guest_downloads_today() >= GUEST_DAILY_LIMIT:
            return False, 'limit'
        return True, None

def downloads_remaining():
    if current_user.is_authenticated:
        if current_user.is_premium:
            return 999
        current_user.reset_if_new_day()
        return max(0, FREE_USER_DAILY_LIMIT - current_user.daily_download_count)
    return max(0, GUEST_DAILY_LIMIT - get_guest_downloads_today())

def fire_webhook(user):
    if not WEBHOOK_URL:
        return
    try:
        http_requests.post(WEBHOOK_URL, json={
            'username': user.username,
            'email': user.email,
            'signed_up_at': user.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        }, timeout=5)
    except Exception:
        pass

def cobalt_download(url, mode, job_id, job_dir):
    """Download using cobalt API"""
    # Map mode to cobalt downloadMode
    download_mode = 'audio' if mode in ('mp3', 'convert') else 'auto'
    audio_format = 'mp3' if mode in ('mp3', 'convert') else 'best'

    payload = {
        'url': url,
        'downloadMode': download_mode,
        'audioFormat': audio_format,
        'videoQuality': '1080',
    }

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }

    # Try each cobalt instance
    cobalt_url = None
    cobalt_data = None
    for instance in COBALT_INSTANCES:
        try:
            jobs[job_id].update({'status': 'downloading', 'percent': 10, 'message': 'Connecting to download service...'})
            res = http_requests.post(f"{instance}/", json=payload, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                if data.get('status') in ('stream', 'redirect', 'tunnel'):
                    cobalt_url = data.get('url')
                    cobalt_data = data
                    break
                elif data.get('status') == 'picker':
                    # Multiple streams, pick first
                    cobalt_url = data.get('picker', [{}])[0].get('url')
                    break
        except Exception:
            continue

    if not cobalt_url:
        raise Exception("Could not connect to download service. Please try again.")

    # Download the actual file
    jobs[job_id].update({'status': 'downloading', 'percent': 30, 'message': 'Downloading...'})
    ext = 'mp3' if mode in ('mp3', 'convert') else 'mp4'
    filename = f"download.{ext}"

    file_res = http_requests.get(cobalt_url, stream=True, timeout=120)
    file_res.raise_for_status()

    # Try to get filename from headers
    cd = file_res.headers.get('Content-Disposition', '')
    if 'filename=' in cd:
        filename = cd.split('filename=')[-1].strip('"\'')
        if not filename.endswith(f'.{ext}'):
            filename = filename.rsplit('.', 1)[0] + f'.{ext}'

    filepath = job_dir / filename
    total = int(file_res.headers.get('Content-Length', 0))
    downloaded = 0

    with open(filepath, 'wb') as f:
        for chunk in file_res.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = int(30 + (downloaded / total) * 65)
                    speed = downloaded / 1024 / 1024
                    jobs[job_id].update({
                        'status': 'downloading',
                        'percent': percent,
                        'message': f'Downloading... {percent}%',
                        'speed': f'{speed:.1f} MB'
                    })

    return filepath, filename

def download_worker(job_id, url, mode, user_id=None):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    try:
        filepath, filename = cobalt_download(url, mode, job_id, job_dir)
        title = filename.rsplit('.', 1)[0]

        jobs[job_id].update({
            'status': 'done', 'percent': 100, 'message': 'Done!',
            'filename': filename, 'filepath': str(filepath), 'title': title
        })

        with app.app_context():
            if user_id:
                user = User.query.get(user_id)
                if user:
                    user.reset_if_new_day()
                    user.daily_download_count += 1
                    user.last_download_date = date.today()
                    dl = Download(user_id=user_id, title=title, mode=mode)
                    db.session.add(dl)
                    db.session.commit()
    except Exception as e:
        jobs[job_id].update({'status': 'error', 'percent': 0, 'message': str(e)})

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not email or not username or not password:
            return render_template('signup.html', error='All fields are required.')
        if User.query.filter_by(email=email).first():
            return render_template('signup.html', error='Email already registered.')
        if User.query.filter_by(username=username).first():
            return render_template('signup.html', error='Username taken.')
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(email=email, username=username, password=hashed)
        db.session.add(user)
        db.session.commit()
        fire_webhook(user)
        login_user(user)
        return redirect(url_for('index'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid email or password.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/')
def index():
    dl_left = downloads_remaining()
    is_guest = not current_user.is_authenticated
    return render_template('index.html',
        downloads_left=dl_left,
        is_guest=is_guest,
        ad_url=AD_YOUTUBE_URL,
        guest_limit=GUEST_DAILY_LIMIT,
        free_limit=FREE_USER_DAILY_LIMIT)

@app.route('/dashboard')
@login_required
def dashboard():
    recent = Download.query.filter_by(user_id=current_user.id).order_by(Download.created_at.desc()).limit(20).all()
    dl_left = downloads_remaining()
    return render_template('dashboard.html', recent=recent, downloads_left=dl_left)

@app.route('/api/info', methods=['POST'])
def get_info():
    url = request.json.get('url', '').strip()
    try:
        # Use cobalt to get info
        for instance in COBALT_INSTANCES:
            try:
                res = http_requests.post(f"{instance}/", json={'url': url, 'downloadMode': 'auto'},
                                         headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    return jsonify({
                        'title': data.get('filename', 'YouTube Video').rsplit('.', 1)[0],
                        'thumbnail': None,
                        'duration': None,
                        'uploader': 'YouTube',
                        'view_count': None
                    })
            except Exception:
                continue
        return jsonify({'error': 'Could not fetch video info'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.json
    url = data.get('url', '').strip()
    mode = data.get('mode', 'mp3')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    allowed, reason = can_download()
    if not allowed:
        return jsonify({'error': 'limit', 'message': 'Daily limit reached!'}), 403
    if not current_user.is_authenticated:
        session['guest_downloads'] = get_guest_downloads_today() + 1
        session['guest_dl_date'] = str(date.today())
    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'starting', 'percent': 0, 'message': 'Starting...'}
    user_id = current_user.id if current_user.is_authenticated else None
    thread = threading.Thread(target=download_worker, args=(job_id, url, mode, user_id))
    thread.daemon = True
    thread.start()
    show_ad = not current_user.is_authenticated
    return jsonify({'job_id': job_id, 'show_ad': show_ad})

@app.route('/api/status/<job_id>')
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)

@app.route('/api/file/<job_id>')
def serve_file(job_id):
    job = jobs.get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'File not ready'}), 404
    filepath = job.get('filepath')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    return send_file(filepath, as_attachment=True, download_name=job.get('filename'))

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    print("\n✅  YTDL running at: http://localhost:5100\n")
    app.run(host='0.0.0.0', port=5100, debug=False)
