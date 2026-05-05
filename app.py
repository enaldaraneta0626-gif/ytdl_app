from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import yt_dlp
import os
import threading
import uuid
import requests as http_requests
from pathlib import Path
from datetime import datetime, date

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///ytdl.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
# ✏️  CHANGE YOUR AD VIDEO URL HERE ANYTIME — no other changes needed
AD_YOUTUBE_URL = "https://www.youtube.com/watch?v=7tZAhO7BEnA"
# ─────────────────────────────────────────────────────────────

GUEST_DAILY_LIMIT = 5
FREE_USER_DAILY_LIMIT = 10

# Set this in Render environment variables once you have a Make/Zapier webhook
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')

jobs = {}

# ── Models ────────────────────────────────────────────────────────────────────

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

    def get_downloads_today(self):
        if self.last_download_date != date.today():
            return 0
        return self.daily_download_count

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

# ── Helpers ───────────────────────────────────────────────────────────────────

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

def get_ydl_opts(job_id, mode, output_path):
    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            percent = int((downloaded / total) * 100) if total else 0
            speed = d.get('speed', 0)
            speed_str = f"{speed/1024/1024:.1f} MB/s" if speed else "..."
            jobs[job_id].update({'status': 'downloading', 'percent': percent, 'speed': speed_str, 'message': f'Downloading... {percent}%'})
        elif d['status'] == 'finished':
            jobs[job_id].update({'status': 'processing', 'percent': 95, 'message': 'Processing...'})

    cookies_file = Path(__file__).parent / 'cookies.txt'
    cookie_arg = str(cookies_file) if cookies_file.exists() else None
    base = {'outtmpl': str(output_path / '%(title)s.%(ext)s'), 'progress_hooks': [progress_hook], 'noplaylist': True, 'cookiefile': cookie_arg}
    if mode == 'mp3':
        return {**base, 'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]}
    elif mode == 'mp4':
        return {**base, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', 'merge_output_format': 'mp4'}
    elif mode == 'convert':
        return {**base, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', 'merge_output_format': 'mp4',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]}

def download_worker(job_id, url, mode, user_id=None):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    try:
        opts = get_ydl_opts(job_id, mode, job_dir)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'download')
        files = list(job_dir.iterdir())
        if not files:
            raise Exception("No file was downloaded.")
        jobs[job_id].update({'status': 'done', 'percent': 100, 'message': 'Done!',
                             'filename': files[0].name, 'filepath': str(files[0]), 'title': title})
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

# ── Auth Routes ───────────────────────────────────────────────────────────────

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

# ── Main Routes ───────────────────────────────────────────────────────────────

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

# ── API Routes ────────────────────────────────────────────────────────────────

@app.route('/api/info', methods=['POST'])
def get_info():
    url = request.json.get('url', '').strip()
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'noplaylist': True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify({'title': info.get('title'), 'thumbnail': info.get('thumbnail'),
                        'duration': info.get('duration'), 'uploader': info.get('uploader'),
                        'view_count': info.get('view_count')})
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

    # Guests see the ad popup, logged-in free/premium users don't
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

# Create tables on startup — required for Render/gunicorn
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    print("\n✅  YTDL running at: http://localhost:5100\n")
    app.run(host='0.0.0.0', port=5100, debug=False)
