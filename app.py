import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import uuid
import tempfile
from datetime import datetime
import json

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
mail = Mail(app)

# Configure email settings
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER')

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    is_verified = db.Column(db.Boolean, default=True)  # Changed from False to True
    verification_token = db.Column(db.String(100), nullable=True)
    documents = db.relationship('Document', backref='owner', lazy=True)
    chat_history = db.relationship('ChatHistory', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(255), nullable=False)
    upload_date = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    extracted_text = db.Column(db.Text, nullable=True)
    
class ChatHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    document_id = db.Column(db.Integer, db.ForeignKey('document.id'), nullable=True)
    query = db.Column(db.Text, nullable=False)
    response = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        # Check if user already exists
        if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first():
            flash('Username or email already exists')
            return redirect(url_for('register'))
        
        # Create new user
        user = User(username=username, email=email)
        user.set_password(password)
        # Remove verification token assignment
        
        db.session.add(user)
        db.session.commit()
        
        # Remove email verification sending
        
        flash('Registration successful! You can now log in.')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/verify-email/<token>')
def verify_email(token):
    user = User.query.filter_by(verification_token=token).first()
    if user:
        user.is_verified = True
        user.verification_token = None
        db.session.commit()
        flash('Email verified! You can now log in.')
    else:
        flash('Invalid verification link.')
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            # Remove verification check
            
            login_user(user)
            return redirect(url_for('dashboard'))
        
        flash('Invalid username or password')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    documents = Document.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', documents=documents)

@app.route('/upload', methods=['POST'])
@login_required
def upload_document():
    if 'file' not in request.files:
        flash('No file part')
        return redirect(url_for('dashboard'))
    
    file = request.files['file']
    
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('dashboard'))
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{current_user.id}_{uuid.uuid4()}_{filename}")
        file.save(file_path)
        
        # Create document record
        document = Document(
            filename=filename,
            file_path=file_path,
            user_id=current_user.id
        )
        
        db.session.add(document)
        db.session.commit()
        
        # Process the document in the background (in a real app, use a task queue)
        process_document(document.id)
        
        flash('Document uploaded successfully')
        return redirect(url_for('dashboard'))
    
    flash('Invalid file type')
    return redirect(url_for('dashboard'))

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'pdf'}

@app.route('/chat/<int:document_id>')
@login_required
def chat(document_id):
    document = Document.query.get_or_404(document_id)
    
    # Ensure the document belongs to the current user
    if document.user_id != current_user.id:
        flash('Access denied')
        return redirect(url_for('dashboard'))
    
    return render_template('chat.html', document=document)

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.json
    document_id = data.get('document_id')
    query = data.get('query')
    
    if not document_id or not query:
        return jsonify({'error': 'Missing document_id or query'}), 400
    
    document = Document.query.get_or_404(document_id)
    
    # Ensure the document belongs to the current user
    if document.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    # Process the query using the LLM
    response = process_query(document, query)
    
    # Save to chat history
    chat_history = ChatHistory(
        user_id=current_user.id,
        document_id=document_id,
        query=query,
        response=response
    )
    db.session.add(chat_history)
    db.session.commit()
    
    return jsonify({'response': response})

@app.route('/history')
@login_required
def history():
    chat_history = ChatHistory.query.filter_by(user_id=current_user.id).order_by(ChatHistory.timestamp.desc()).all()
    return render_template('history.html', chat_history=chat_history)

@app.route('/conformity-matrix')
@login_required
def conformity_matrix():
    return render_template('conformity_matrix.html')

@app.route('/data-processing')
@login_required
def data_processing():
    return render_template('data_processing.html')

@app.route('/test-plan')
@login_required
def test_plan():
    return render_template('test_plan.html')

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html')

# Helper functions for document processing and LLM integration
def process_document(document_id):
    """Process the uploaded document to extract text and prepare for RAG"""
    document = Document.query.get(document_id)
    if not document:
        return
    
    # Implementation will be added based on the PDF extraction code provided
    # This is a placeholder for now
    document.extracted_text = "Extracted text will be here"
    db.session.commit()

def process_query(document, query):
    """Process a user query against a document using LLM and RAG"""
    # This is a placeholder - actual implementation will use the LLM code provided
    return f"This is a simulated response to: {query}"

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
