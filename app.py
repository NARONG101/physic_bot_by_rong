import os
import io
import json
import datetime
import asyncio
import re 
import time
import uuid
import html
from collections import deque
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes
import PIL.Image
from filelock import FileLock 
import matplotlib.pyplot as plt
import numpy as np

# 🚀 Import our hybrid SQLite user manager
import user_db
from history_retention import filter_retention, run_history_retention_once
from dataset_rag import retrieve_relevant_context, try_auto_learn_from_chat

# --- Configuration ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 12345678)) 

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("🚨 Missing API Keys! Please check your .env file.")

client = genai.Client(api_key=GEMINI_API_KEY)

# --- 🚀 PATHS CONFIGURATION ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_DIR = os.path.join(CURRENT_DIR, 'Json')
UPLOAD_DIR = os.path.join(CURRENT_DIR, 'uploads')

os.makedirs(JSON_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

HISTORY_FILE = os.path.join(JSON_DIR, 'chat_history.json')
KNOWLEDGE_FILE = os.path.join(JSON_DIR, 'Data_Training.json')
SETTINGS_FILE = os.path.join(JSON_DIR, 'bot_settings.json')
USAGE_FILE = os.path.join(JSON_DIR, 'usage_tracking.json') 
FEEDBACK_FILE = os.path.join(JSON_DIR, 'user_feedback.json')

# --- 🚦 GLOBAL RATE LIMITER & SPAM TRACKER ---
api_request_times = deque(maxlen=15)
api_rate_lock = asyncio.Lock()
user_last_active = {}

async def wait_for_rate_limit():
    """Ensures the bot never exceeds Google's 15 RPM Free Tier limit."""
    async with api_rate_lock:
        now = time.time()
        while api_request_times and now - api_request_times[0] > 60:
            api_request_times.popleft()

        if len(api_request_times) >= 15:
            sleep_time = 60 - (now - api_request_times[0])
            if sleep_time > 0:
                print(f"🚦 Rate limit approached! Pausing for {sleep_time:.2f} seconds...")
                await asyncio.sleep(sleep_time)
                now = time.time()
        
        api_request_times.append(now)

# --- ⚙️ Read Dynamic Settings from Admin Panel ---
def get_bot_settings():
    defaults = {
        "status": "online", "free_limit": 10, "premium_limit": 40, "temperature": 0.2,
        "cooldown_seconds": 2, "max_input_length": 1000,
        "model_name": "gemini-3.1-flash-lite-preview",
        "api_retry_attempts": 3,
        "api_retry_base_delay": 4,
        "system_prompt": "You are Phy_Chatbot, an expert physics tutor.",
        "welcome_message": "Welcome to Phy_Chatbot! ⚛️ Send me a physics question.",
        "maintenance_message": "The bot is under maintenance. Please check back later!",
        "features": {
            "free": {"image_analysis": False, "step_by_step": True, "pdf_reading": False, "generate_quizzes": False},
            "premium": {"image_analysis": True, "step_by_step": True, "pdf_reading": True, "generate_quizzes": True}
        }
    }
    
    lock_path = SETTINGS_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    loaded_data = json.load(f)
                    for key, value in loaded_data.items():
                        if isinstance(value, dict) and key in defaults:
                            defaults[key].update(value)
                        else:
                            defaults[key] = value
            except Exception as e: 
                print(f"Settings Load Error: {e}")
    return defaults

def can_use_feature(plan, feature_name, settings):
    plan_key = "premium" if plan == "Premium" else "free"
    return settings.get("features", {}).get(plan_key, {}).get(feature_name, False)


def get_runtime_model(settings):
    model_name = (settings.get("model_name") or "").strip()
    return model_name or "gemini-3.1-flash-lite-preview"


def get_retry_params(settings):
    attempts = max(1, min(6, int(settings.get("api_retry_attempts", 3))))
    base_delay = max(1, min(15, int(settings.get("api_retry_base_delay", 4))))
    return attempts, base_delay

# --- 👥 12-Hour Cycle User Management ---
async def check_and_update_limit(user_id, username):
    settings = get_bot_settings()
    plan = user_db.get_user_plan(user_id, username or "Unknown")
    limit = settings.get('premium_limit', 40) if plan == "Premium" else settings.get('free_limit', 10)
    
    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    cycle_key = f"{date_str}-AM" if now.hour < 12 else f"{date_str}-PM"
    uid_str = str(user_id)
    
    lock_path = USAGE_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        usage_data = {}
        if os.path.exists(USAGE_FILE):
            try:
                with open(USAGE_FILE, 'r', encoding='utf-8') as f:
                    usage_data = json.load(f)
            except: pass

        user_usage = usage_data.get(uid_str, {"count": 0, "last_cycle": ""})

        if user_usage.get("last_cycle") != cycle_key:
            user_usage["count"] = 0
            user_usage["last_cycle"] = cycle_key

        if user_usage["count"] >= limit:
            return False, user_usage["count"], limit, plan

        user_usage["count"] += 1
        usage_data[uid_str] = user_usage
        with open(USAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(usage_data, f, indent=4)

    return True, user_usage["count"], limit, plan

# --- Helper Functions ---
def save_to_history(user_id, username, user_msg, bot_msg, image_filename=None):
    new_entry = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "username": username or "Unknown",
        "user_input": user_msg,
        "bot_response": bot_msg
    }
    if image_filename:
        new_entry["image_file"] = image_filename
        
    lock_path = HISTORY_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        data = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except: pass
        data.append(new_entry)
        data, _removed = filter_retention(data)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def load_knowledge_base(chapter_keyword=None):
    lock_path = KNOWLEDGE_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if os.path.exists(KNOWLEDGE_FILE):
            try:
                with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    if not chapter_keyword:
                        # Return a summary/index of chapters instead of full text if no keyword
                        summary = "KNOWLEDGE BASE INDEX:\n"
                        for grade_key, grade_info in data.items():
                            # Skip non-dict entries (like past_examples which might be a list)
                            if not isinstance(grade_info, dict):
                                continue
                            summary += f"- {grade_info.get('grade')}:\n"
                            for chapter in grade_info.get('chapters', []):
                                summary += f"  * {chapter.get('chapter_name')}\n"
                        return summary
                    
                    # Search for relevant chapter/lesson
                    relevant_parts = []
                    chapter_keyword = chapter_keyword.lower()
                    
                    # First, try to find exact grade match if user specified a grade
                    target_grade = None
                    if "៩" in chapter_keyword or "9" in chapter_keyword or "ទី៩" in chapter_keyword:
                        target_grade = "Grade_9"
                    elif "៨" in chapter_keyword or "8" in chapter_keyword or "ទី៨" in chapter_keyword:
                        target_grade = "Grade_8"
                    elif "៧" in chapter_keyword or "7" in chapter_keyword or "ទី៧" in chapter_keyword:
                        target_grade = "Grade_7"
                    
                    # Search through all grades (or target grade if specified)
                    for grade_key, grade_info in data.items():
                        # Skip non-dict entries
                        if not isinstance(grade_info, dict):
                            continue
                        
                        # If user specified a grade, only search that grade
                        if target_grade and grade_key != target_grade:
                            continue
                        
                        for chapter in grade_info.get('chapters', []):
                            if chapter_keyword in chapter.get('chapter_name', '').lower():
                                relevant_parts.append(chapter)
                                continue
                            for lesson in chapter.get('lessons', []):
                                if chapter_keyword in lesson.get('title', '').lower():
                                    relevant_parts.append(lesson)
                    
                    if relevant_parts:
                        return json.dumps(relevant_parts, ensure_ascii=False)
                    
                    # If no match found, return the specific grade's content (or all if no grade specified)
                    if target_grade:
                        # Return that specific grade's first chapter
                        grade_data = data.get(target_grade, {})
                        fallback = {
                            target_grade: grade_data.get("chapters", [{}])[0]
                        }
                    else:
                        # No grade specified, return all three grades' first chapters
                        fallback = {
                            "Grade_7": data.get("Grade_7", {}).get("chapters", [{}])[0],
                            "Grade_8": data.get("Grade_8", {}).get("chapters", [{}])[0],
                            "Grade_9": data.get("Grade_9", {}).get("chapters", [{}])[0]
                        }
                    return json.dumps(fallback, ensure_ascii=False)
            except Exception as e:
                print(f"Knowledge Load Error: {e}")
                return "Error loading knowledge base."
    return "No training data found."

def improve_math_font(text):
    """
    Convert LaTeX to Unicode and remove $ signs while preserving formatting.
    Keeps the structure and line breaks intact.
    """
    
    # Process content within $ delimiters first
    def process_math(match):
        """Convert LaTeX math to Unicode, keeping the content but removing $"""
        math_content = match.group(1)
        return convert_latex_to_unicode(math_content)
    
    # Handle $...$ patterns (inline math)
    text = re.sub(r'\$([^$]+)\$', process_math, text)
    
    # Handle $$...$$ patterns (display math)
    text = re.sub(r'\$\$([^$]+)\$\$', process_math, text)
    
    # Clean up any remaining LaTeX commands not caught by $ delimiters
    text = convert_latex_to_unicode(text)
    text = text.replace('÷', '/')
    
    return text


def enforce_dataset_symbols(answer_text: str, retrieved_context: str, user_query: str = "") -> str:
    """
    Enforce dataset symbol fidelity on the final answer.
    Always rewrite voltage-as-U patterns (U, U_PN, U_s, etc.) to V to match
    the Khmer curriculum dataset which exclusively uses V for voltage (តង់ស្យុង).
    """
    ans = answer_text or ""

    def _swap_u_to_v_line(line: str) -> str:
        # Only adjust lines that look like math/formulas to avoid touching Khmer words.
        if not any(ch in line for ch in ["=", "×", "/", "Ω", "A", "V", "W", "Q", "R", "I", "U"]):
            return line
        # Common voltage notations the model tends to output (be permissive; math-lines only)
        line = line.replace("U_total", "V_total").replace("Utotal", "Vtotal")
        line = line.replace("U_PN", "V_PN").replace("U_pn", "V_pn")
        line = line.replace("U_s", "V_s").replace("Uₛ", "Vₛ")
        line = line.replace("U0", "V0").replace("U₀", "V₀")
        line = line.replace("U_motor", "V_motor").replace("U_moto", "V_moto")

        # Replace U with unicode subscripts: U₁, U₂, ... and also U followed by subscript letters
        subscript_chars = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"
        line = re.sub(rf"U(?=[{re.escape(subscript_chars)}])", "V", line)

        # Replace U when used like a symbol token in equations (U = ..., I = U/R, etc.)
        # Match U if it's followed by whitespace, punctuation, operators, end, or an underscore-name.
        line = re.sub(r"U(?=$|[\s=×/)\],.;:+-])", "V", line)
        line = re.sub(r"U(?=_[A-Za-z0-9]+)", "V", line)
        return line

    return "\n".join(_swap_u_to_v_line(ln) for ln in ans.splitlines())


def convert_latex_to_unicode(text):
    """
    Helper function: Convert all LaTeX commands to Unicode symbols.
    """
    # Handle \frac{a}{b} -> a/b format
    text = re.sub(r'\\frac\s*\{\s*([^}]+)\s*\}\s*\{\s*([^}]+)\s*\}', r'\1/\2', text)
    
    # Handle \sqrt{x} -> √x format
    text = re.sub(r'\\sqrt\s*\{\s*([^}]+)\s*\}', r'√\1', text)
    text = re.sub(r'\\sqrt\s+(\S+)', r'√\1', text)
    
    # Handle \left( \right) and other delimiters
    text = text.replace(r'\left(', '(').replace(r'\right)', ')')
    text = text.replace(r'\left[', '[').replace(r'\right]', ']')
    text = text.replace(r'\left\{', '{').replace(r'\right\}', '}')
    text = text.replace(r'\left|', '|').replace(r'\right|', '|')
    
    # Comprehensive LaTeX to Unicode symbol mapping
    latex_symbols = {
        # Degree and special symbols
        r'\degree': '°', r'\circ': '°', r'^\circ': '°', r'^{\circ}': '°',
        
        # Operators
        r'\times': '×', r'\cdot': '·', r'\div': '/', r'\ast': '∗',
        r'\pm': '≈', r'\mp': '≈', r'\sqrt': '√', r'\root': '∛',
        
        # Relations
        r'\approx': '≈', r'\cong': '≅', r'\neq': '≠', r'\equiv': '≡',
        r'\leq': '≤', r'\geq': '≥', r'\ll': '≪', r'\gg': '≫',
        r'\subset': '⊂', r'\supset': '⊃', r'\subseteq': '⊆', r'\supseteq': '⊇',
        r'\in': '∈', r'\notin': '∉', r'\cap': '∩', r'\cup': '∪',
        r'\perp': '⊥', r'\parallel': '∥', r'\propto': '∝',
        
        # Arrows
        r'\rightarrow': '→', r'\leftarrow': '←', r'\leftrightarrow': '↔',
        r'\Rightarrow': '⇒', r'\Leftarrow': '⇐', r'\Leftrightarrow': '⇔',
        r'\uparrow': '↑', r'\downarrow': '↓', r'\updownarrow': '↕',
        
        # Greek letters (lowercase)
        r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ',
        r'\epsilon': 'ε', r'\varepsilon': 'ε', r'\zeta': 'ζ', r'\eta': 'η',
        r'\theta': 'θ', r'\vartheta': 'ϑ', r'\iota': 'ι', r'\kappa': 'κ',
        r'\lambda': 'λ', r'\mu': 'μ', r'\nu': 'ν', r'\xi': 'ξ',
        r'\omicron': 'ο', r'\pi': 'π', r'\varpi': 'ϖ', r'\rho': 'ρ',
        r'\varrho': 'ϱ', r'\sigma': 'σ', r'\varsigma': 'ς', r'\tau': 'τ',
        r'\upsilon': 'υ', r'\phi': 'φ', r'\varphi': 'φ', r'\chi': 'χ',
        r'\psi': 'ψ', r'\omega': 'ω',
        
        # Greek letters (uppercase)
        r'\Alpha': 'Α', r'\Beta': 'Β', r'\Gamma': 'Γ', r'\Delta': 'Δ',
        r'\Epsilon': 'Ε', r'\Zeta': 'Ζ', r'\Eta': 'Η', r'\Theta': 'Θ',
        r'\Iota': 'Ι', r'\Kappa': 'Κ', r'\Lambda': 'Λ', r'\Mu': 'Μ',
        r'\Nu': 'Ν', r'\Xi': 'Ξ', r'\Omicron': 'Ο', r'\Pi': 'Π',
        r'\Rho': 'Ρ', r'\Sigma': 'Σ', r'\Tau': 'Τ', r'\Upsilon': 'Υ',
        r'\Phi': 'Φ', r'\Chi': 'Χ', r'\Psi': 'Ψ', r'\Omega': 'Ω',
        r'\ohm': 'Ω', r'\oum': 'Ω',
        
        # Mathematical constants
        r'\infty': '∞', r'\partial': '∂', r'\nabla': '∇', 
        r'\forall': '∀', r'\exists': '∃', r'\nexists': '∄', 
        r'\neg': '¬', r'\therefore': '∴',
        
        # Set theory & Logic
        r'\emptyset': '∅', r'\varnothing': '∅', r'\complement': '∁',
        r'\aleph': 'ℵ', r'\beth': 'ℶ',
        
        # Calculus
        r'\sum': '∑', r'\prod': '∏', r'\int': '∫', r'\iint': '∬',
        r'\iiint': '∭', r'\oint': '∮',
        
        # Other
        r'\prime': '′', r'\ast': '∗', r'\dagger': '†',
        r'\dag': '†', r'\ddagger': '‡', r'\ddag': '‡',
    }
    
    # Apply LaTeX symbol replacements (in order of length to avoid partial replacements)
    for latex_cmd in sorted(latex_symbols.keys(), key=len, reverse=True):
        text = text.replace(latex_cmd, latex_symbols[latex_cmd])
    
    # Create comprehensive subscript and superscript mappings
    superscript_chars = {
        '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵',
        '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹', '+': '⁺', '-': '⁻',
        '=': '⁼', '(': '⁽', ')': '⁾', 'n': 'ⁿ', 'x': 'ˣ', 'y': 'ʸ',
        'a': 'ᵃ', 'b': 'ᵇ', 'c': 'ᶜ', 'd': 'ᵈ', 'e': 'ᵉ', 'f': 'ᶠ',
        'g': 'ᵍ', 'h': 'ʰ', 'i': 'ⁱ', 'j': 'ʲ', 'k': 'ᵏ', 'l': 'ˡ',
        'm': 'ᵐ', 'o': 'ᵒ', 'p': 'ᵖ', 'r': 'ʳ', 's': 'ˢ', 't': 'ᵗ',
        'u': 'ᵘ', 'v': 'ᵛ', 'w': 'ʷ', 'z': 'ᶻ'
    }
    
    subscript_chars = {
        '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄', '5': '₅',
        '6': '₆', '7': '₇', '8': '₈', '9': '₉', '+': '₊', '-': '₋',
        '=': '₌', '(': '₍', ')': '₎', 'a': 'ₐ', 'e': 'ₑ', 'h': 'ₕ',
        'i': 'ᵢ', 'j': 'ⱼ', 'k': 'ₖ', 'l': 'ₗ', 'm': 'ₘ', 'n': 'ₙ',
        'o': 'ₒ', 'p': 'ₚ', 'r': 'ᵣ', 's': 'ₛ', 't': 'ₜ', 'u': 'ᵤ',
        'v': 'ᵥ', 'x': 'ₓ'
    }
    
    # Convert LaTeX superscripts: ^{...} or ^X patterns
    text = re.sub(r'\^\{([^}]+)\}', lambda m: ''.join(
        superscript_chars.get(c, c) for c in m.group(1)
    ), text)
    text = re.sub(r'\^([0-9a-zA-Z+\-=()\s])', lambda m: 
        superscript_chars.get(m.group(1), m.group(1)), text)
    
    # Convert LaTeX subscripts: _{...} or _X patterns  
    text = re.sub(r'_\{([^}]+)\}', lambda m: ''.join(
        subscript_chars.get(c, c) for c in m.group(1)
    ), text)
    text = re.sub(r'_([0-9a-zA-Z+\-=()\s])', lambda m: 
        subscript_chars.get(m.group(1), m.group(1)), text)
    
    # Handle \text{...}, \rm{...}, \mathrm{...} and other text commands
    text = re.sub(r'\\text\s*\{\s*([^}]+)\s*\}', r'\1', text)
    text = re.sub(r'\\mathrm\s*\{\s*([^}]+)\s*\}', r'\1', text)
    text = re.sub(r'\\rm\s*\{\s*([^}]+)\s*\}', r'\1', text)
    text = re.sub(r'\\mbox\s*\{\s*([^}]+)\s*\}', r'\1', text)
    
    # Clean up multiple backslashes
    text = text.replace('\\\\', ' ')
    
    # Remove any remaining LaTeX commands that might have been missed
    text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
    
    return text

def get_chat_session(user_id, plan_type, settings, extra_knowledge: str = ""):
    knowledge_base_text = load_knowledge_base()
    custom_persona = settings.get("system_prompt", "You are an expert Physics Teacher.")
    retrieved = (extra_knowledge or "").strip()
    if retrieved:
        knowledge_block = (
            f"{knowledge_base_text}\n\n"
            "📌 RELEVANT EXCERPTS (retrieved from Data_Training.json for this request — match tone, notation, and difficulty):\n"
            f"{retrieved}\n"
        )
    else:
        knowledge_block = knowledge_base_text

    base_prompt = (
        f"{custom_persona}\n"
        "You communicate ENTIRELY in Khmer (except for standard physics variables and formulas).\n\n"
        "📚 YOUR CURRICULUM INDEX + MATERIAL:\n"
        f"{knowledge_block}\n\n"
        "🎯 INSTRUCTIONS:\n"
        "1. Ground every explanation in the curriculum material and retrieved excerpts above. If something is not covered there, say so briefly and still give a correct physics answer consistent with the course level.\n"
        "2. 📚 SYMBOL FIDELITY RULE (VERY STRICT):\n"
        "   - For each topic, use variable letters/symbols exactly as written in dataset formulas/definitions.\n"
        "   - VOLTAGE SYMBOL: ALWAYS use V (not U) for voltage/តង់ស្យុង throughout ALL answers, formulas, and calculations. This is the standard symbol used in the Khmer physics curriculum.\n"
        "   - Terminal voltage of a battery/generator uses V_PN (not U_PN): V_PN = E - r × I\n"
        "   - Before calculating, include a short mapping line for symbols from dataset (e.g., V = ..., I = ..., R = ...).\n"
        "   - If two notations exist, prefer the notation shown in the retrieved excerpt for this question.\n"
        "3. 📐 MATHEMATICS FORMATTING RULE:\n"
        "   - DO NOT use $ dollar signs anywhere in your response\n"
        "   - Write equations directly using proper Unicode mathematical symbols\n"
        "   - Use multiplication symbol: × (not 'times' or '*')\n"
        "   - Use slash for division: / (not ÷)\n"
        "   - Use almost equals symbol: ≈ for approximate values\n"
        "   - Use proper Unicode Greek letters: α, β, γ, Δ, π, θ, ω, Ω, ε, λ, μ, ρ, σ, τ, φ, χ, ψ\n"
        "   - Use subscripts for multiple quantities: m₁, m₂, v₀, v₁, t₁ (use underscore in text)\n"
        "   - Use superscripts for powers: x² for x to the power of 2 (use caret ^ in text)\n"
        "   - Example equations WITHOUT dollar signs:\n"
        "      Q = m × c × Δt\n"
        "      v² = v₀² + 2as\n"
        "      F = G × (m₁ × m₂)/r²\n"
        "      E = mc²\n"
        "   - NEVER use \\\\frac, \\\\times, \\\\Delta, \\\\circ - convert these to actual symbols\n"
        "4. VARIABLE NAMING: Use numeric subscripts for multiple quantities: m₁, m₂, v₀, v₁, t₁\n"
        "5. 📊 MATH GRAPHS: If the problem requires a visual Cartesian graph (x/y axis), output EXACTLY this tag: [PLOT: <equation>] where <equation> is python-readable math using 'x' (e.g., [PLOT: x**2 + 5]). Do not attempt to draw the graph with text/symbols.\n"
        # 🚀 NEW: Instruction for conceptual Heat Diagrams
        "6. 🌡️ HEAT TRANSFER DIAGRAMS: If the problem is about mixing hot and cold substances (Calorimetry/កាឡូរីមាត្រ), YOU MUST draw a vertical temperature diagram using text characters BEFORE the calculation. Use this exact format, replacing the bracketed values with the actual temperatures:\n\n"
        "[T_hot]°C\n"
        " │  ↓ Q1 (កម្តៅបញ្ចេញ)\n"
        " │\n"
        "[T_final]°C\n"
        " │  ↑ Q2 (កម្តៅស្រូប)\n"
        " │\n"
        "[T_cold]°C\n\n"
        # 🚀 NEW: MULTI-PART SOLVING - CRITICAL IMPROVEMENT
        "7. 🔢 MULTIPLE THINGS TO FIND (STRICT STYLE): If one question asks for multiple results, solve ONE BY ONE and use this exact Khmer-teacher structure.\n\n"
        "   Start with a short greeting line:\n"
        "   សួស្តី! នេះគឺជាដំណោះស្រាយសម្រាប់លំហាត់របស់អ្នក៖\n\n"
        "   Then split by parts using: ក. / ខ. / គ. ...\n"
        "   Inside each part, if there are sub-results, you MUST use '+ រក ...' for each sub-result.\n\n"
        "   Required format for each solved item:\n"
        "   + រក (Thing name)\n"
        "   តាមរូបមន្ត៖ (Main formula)\n"
        "   នាំអោយ៖ (Derived formula, if needed)\n"
        "   ដោយ៖\n"
        "   (Known values, one per line)\n"
        "   គេបាន៖ (Step-by-step substitution and calculation)\n"
        "   ដូចនេះ៖ (Final answer with unit)\n\n"
        "   Move to the next item only after finishing the previous one completely.\n"
        "   Use clear separators between major parts (ក., ខ., គ.).\n\n"
    )
    
    if can_use_feature(plan_type, "step_by_step", settings):
        base_prompt += (
            "8. 🏆 FORMATTING RULE (STRICT): Follow this exact answer style for step-by-step solutions:\n"
            "សួស្តី! នេះគឺជាដំណោះស្រាយសម្រាប់លំហាត់របស់អ្នក៖\n\n"
            "For single-find questions:\n"
            "រក៖ (What needs to be calculated)\n"
            "តាមរូបមន្ត៖ (Write formulas clearly)\n"
            "នាំអោយ៖ (Write converted formula if needed)\n"
            "ដោយ៖ (List known variables, one per line)\n"
            "គេបាន៖ (Step-by-step calculation)\n"
            "ដូចនេះ៖ (Final answer with correct units)\n\n"
            "For multi-find questions:\n"
            "Use sections ក. / ខ. / គ. ... and inside each section use '+ រក ...' lines when needed, then solve each one fully before continuing.\n"
        )
    else:
        base_prompt += (
            "8. 🏆 FORMATTING RULE: Provide clear, concise answers with:\n"
            "- The final correct answer with units\n"
            "- A brief explanation of the physics concept (1-2 sentences)\n"
            "- Key formula or principle used\n"
            "Upgrade to Premium for step-by-step solutions!\n"
        )
        
    return client.chats.create(
        model=get_runtime_model(settings),
        config=types.GenerateContentConfig(
            system_instruction=base_prompt,
            temperature=settings.get("temperature", 0.2)
        )
    )

# --- Security Checks ---
async def run_security_checks(update: Update, user_id: int, user_text: str = "") -> bool:
    settings = get_bot_settings()
    
    if settings.get("status") == "maintenance" and user_id != ADMIN_ID:
        msg = settings.get("maintenance_message", "⚠️ ប្រព័ន្ធកំពុងស្ថិតក្នុងការថែទាំ។ សូមរង់ចាំបន្តិចទៀត។")
        await update.message.reply_text(msg)
        return False
        
    if hasattr(user_db, 'is_banned') and user_db.is_banned(user_id):
        await update.message.reply_text("🚫 គណនីរបស់អ្នកត្រូវបានផ្អាកការប្រើប្រាស់។ សូមទាក់ទង Admin។")
        return False

    now = time.time()
    cooldown = settings.get("cooldown_seconds", 2)
    if now - user_last_active.get(user_id, 0) < cooldown and user_id != ADMIN_ID:
        return False 
    user_last_active[user_id] = now
    
    if user_text and len(user_text) > settings.get("max_input_length", 1000):
        await update.message.reply_text(f"⚠️ សំណួររបស់អ្នកវែងពេក។ សូមសរសេរឱ្យខ្លីជាង {settings.get('max_input_length')} តួអក្សរ។")
        return False
        
    return True

async def send_limit_reached_message(update: Update, count, limit, plan, settings):
    now = datetime.datetime.now()
    reset_time = "12:00 PM (ថ្ងៃត្រង់)" if now.hour < 12 else "12:00 AM (ពាក់កណ្តាលអធ្រាត្រ)"
    
    if plan == "Premium":
        msg = f"⏳ សុំទោស! គណនី Premium របស់អ្នកបានដល់កម្រិតកំណត់ហើយ ({count}/{limit} ដង)។ សូមរង់ចាំរហូតដល់ម៉ោង <b>{reset_time}</b>។"
    else:
        msg = (f"⏳ សុំទោស! អ្នកបានប្រើប្រាស់អស់កម្រិតកំណត់ឥតគិតថ្លៃរបស់អ្នកហើយ ({count}/{limit} ដង)។\n\n"
               f"🔄 ប្រព័ន្ធនឹងផ្តល់សិទ្ធិឱ្យអ្នកសួរឡើងវិញនៅម៉ោង <b>{reset_time}</b>។\n\n"
               f"🌟 <b>ចំណាប់អារម្មណ៍ពិសេស:</b> ដំឡើងទៅកាន់គណនី <b>Premium</b> ដើម្បីសួរបានច្រើនជាងមុន និងប្រើប្រាស់មុខងាររូបភាព និង PDF!\n"
               f"📩 សូមទាក់ទងមកកាន់ <a href='https://t.me/narong12/'>@admin</a> ដើម្បីដំឡើងគណនី។")
    
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)
    else:
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)

# --- 🚀 COMMAND HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await run_security_checks(update, user.id): return
    
    plan = user_db.get_user_plan(user.id, user.username or "Unknown")
    settings = get_bot_settings()
    limit = settings.get('premium_limit') if plan == "Premium" else settings.get('free_limit')
    
    welcome_msg = html.escape(settings.get('welcome_message', "សួស្តី! ខ្ញុំជា Phy_Chatbot។"))
    
    msg = (f"{welcome_msg}\n\n"
           f"🔰 ប្រភេទគណនី: <b>{plan}</b>\n"
           f"📊 កម្រិតប្រើប្រាស់: {limit} សារ / ១២ម៉ោង\n\n"
           f"ចុច /help ដើម្បីមើលរបៀបប្រើប្រាស់។")
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await run_security_checks(update, update.effective_user.id): return
    
    user = update.effective_user
    settings = get_bot_settings()
    plan = user_db.get_user_plan(user.id, user.username or "Unknown")
    
    msg = ("🆘 <b>ជំនួយការប្រើប្រាស់ Phy_Chatbot</b>\n\n"
           f"ស្វាគមន៍! អ្នកបច្ចុប្បន្នប្រើប្រាស់ <b>{plan}</b> ។\n\n"
           
           "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
           "<b>📚 មុខងារដែលមាន</b>\n"
           "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
           
           "💬 <b>សរសេរសារ (ឥតគិតថ្លៃ):</b>\n"
           "   • វាយសំណួររូបវិទ្យាលម្អិតៗ\n"
           "   • ឧ: 'តើកម្លាំងទូលាយប៉ុន្មាន?'\n"
           "   • ទទួលបានចម្លើយលម្អិត + ដំណើរការជំហាន\n\n"
    )
    
    if can_use_feature(plan, "image_analysis", settings):
        msg += ("📸 <b>ផ្ញើរូបភាព ✅ (Premium):</b>\n"
                "   • ថតរូបលំហាត់ រូបវិទ្យា\n"
                "   • ខ្ញុំនឹងដោះស្រាយវា\n\n")
    else:
        msg += ("📸 <b>ផ្ញើរូបភាព 🔒 (Premium only):</b>\n"
                "   • មានលក្ខណៈលម្អិត ដំឡើងទៅ Premium\n\n")
    
    msg += ("🎤 <b>សារសំឡេង៖</b>\n"
            "   • មិនមានមុខងារនេះទេ សូមផ្ញើរជាអត្ថបទឬរូបភាពមកកាន់ពួកយើង\n\n")
    
    if can_use_feature(plan, "pdf_reading", settings):
        msg += ("📄 <b>ឯកសារ PDF ✅ (Premium):</b>\n"
                "   • ផ្ញើឯកសារ PDF រូបវិទ្យា\n"
                "   • ខ្ញុំនឹងវិភាគ និងលម្អិត\n\n")
    else:
        msg += ("📄 <b>ឯកសារ PDF 🔒 (Premium only):</b>\n"
                "   • ដំឡើងដើម្បីប្រើប្រាស់ PDF\n\n")
    
    if can_use_feature(plan, "generate_quizzes", settings):
        msg += ("📝 <b>បង្កើតវិញ្ញាសា ✅ (Premium):</b>\n"
                "   • វាយ /quiz ដើម្បីសាកល្បង\n"
                "   • ជ្រើស ជំពូក ឬមេរៀន\n"
                "   • ខ្ញុំបង្កើតសំណួរ ហើយក្រម\n\n")
    else:
        msg += ("📝 <b>បង្កើតវិញ្ញាសា 🔒 (Premium only):</b>\n"
                "   • ដំឡើងទៅ Premium\n\n")
    
    msg += ("<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
            "<b>⌨️ បញ្ជា (Commands)</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
            "/start - ចាប់ផ្តើមប្រើប្រាស់\n"
            "/quiz - សាកល្បងធ្វើតេស្ត\n"
            "/feedback - ផ្ញើមតិយោបល់ទៅកាន់ក្រុមការងារ\n"
            "/profile - ពិនិត្យគណនី\n"
            "/premium - មើលព័ត៌មាន\n"
            "/help - ជំនួយនេះ\n\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n")
           
    await update.message.reply_text(msg, parse_mode='HTML')

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await run_security_checks(update, user.id): return
    
    plan = user_db.get_user_plan(user.id, user.username or "Unknown")
    settings = get_bot_settings()
    limit = settings.get('premium_limit') if plan == "Premium" else settings.get('free_limit')
    
    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    cycle_key = f"{date_str}-AM" if now.hour < 12 else f"{date_str}-PM"
    uid_str = str(user.id)
    count = 0
    
    lock_path = USAGE_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if os.path.exists(USAGE_FILE):
            try:
                with open(USAGE_FILE, 'r', encoding='utf-8') as f:
                    usage_data = json.load(f)
                    user_usage = usage_data.get(uid_str, {})
                    if user_usage.get("last_cycle") == cycle_key:
                        count = user_usage.get("count", 0)
            except: pass

    reset_time = "12:00 PM (ថ្ងៃត្រង់)" if now.hour < 12 else "12:00 AM (ពាក់កណ្តាលអធ្រាត្រ)"
    safe_username = html.escape(user.username or 'Unknown')
    
    msg = (f"👤 <b>ព័ត៌មានគណនីរបស់អ្នក (Your Profile)</b>\n"
           f"━━━━━━━━━━━━━━━━━━\n"
           f"🔹 <b>ឈ្មោះ:</b> @{safe_username}\n"
           f"🔹 <b>ប្រភេទគណនី:</b> {plan}\n"
           f"🔹 <b>ចំនួនសារប្រើរួច:</b> {count} / {limit}\n"
           f"🔹 <b>ម៉ោងកំណត់ឡើងវិញ:</b> {reset_time}\n"
           f"━━━━━━━━━━━━━━━━━━")
    
    if plan != "Premium":
        msg += "\n\n💡 ចុច /premium ដើម្បីទទួលបានចំនួនសារច្រើនជាងនេះ!"
        
    await update.message.reply_text(msg, parse_mode='HTML')

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await run_security_checks(update, update.effective_user.id): return
    
    msg = ("🌟 <b>អត្ថប្រយោជន៍នៃគណនី PREMIUM</b> 🌟\n"
           "━━━━━━━━━━━━━━━━━━\n"
           "✅ <b>ចំនួនសារច្រើនជាងមុន:</b> សួរបានច្រើនដងរៀងរាល់ 12 ម៉ោង។\n"
           "✅ <b>មុខងាររូបភាព (Vision):</b> ថតរូបលំហាត់ឱ្យ AI ដោះស្រាយ។\n"
           "✅ <b>មុខងារ PDF:</b> អាន និងដោះស្រាយលំហាត់ចេញពីឯកសារ PDF។\n"
           "✅ <b>បង្កើតកម្រងសំណួរ (Quiz):</b> ហ្វឹកហាត់ជាមួយសំណួរពិសេសៗ។\n"
           "✅ <b>ការពន្យល់លម្អិត:</b> ទទួលបានការពន្យល់មួយជំហានម្តងៗ (Step-by-step)។\n\n"
           "💳 <b>តម្លៃត្រឹមតែ $2.99 / ខែ ប៉ុណ្ណោះ!t</b>\n"
           "📩 សូមទាក់ទងមកកាន់ <a href='https://t.me/narong12/'>@admin</a> ដើម្បីដំឡើងគណនី។")
           
    await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)


async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await run_security_checks(update, user.id):
        return ConversationHandler.END

    context.user_data.pop("feedback_text", None)
    msg = (
        "💬 <b>Feedback Form</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "សូមសរសេរមតិយោបល់ ឬបទពិសោធន៍ប្រើប្រាស់ bot របស់អ្នក។\n"
        "អ្នកអាចបញ្ចូល៖\n"
        "• អ្វីដែលអ្នកពេញចិត្ត\n"
        "• អ្វីដែលគួរកែប្រែ\n"
        "• មុខងារដែលអ្នកចង់បានបន្ថែម\n\n"
        "បន្ទាប់ពីសរសេរ អ្នកអាចបន្ថែមរូបភាពផងដែរ។\n"
        "⚠️ វាយ /cancel ដើម្បីបោះបង់។"
    )
    await update.message.reply_text(msg, parse_mode='HTML')
    return WAITING_FOR_FEEDBACK_TEXT


async def feedback_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    feedback_text = (update.message.text or "").strip()

    if not await run_security_checks(update, user.id, feedback_text):
        return ConversationHandler.END

    if len(feedback_text) < 5:
        await update.message.reply_text("សូមសរសេរ feedback ឱ្យបានលម្អិតបន្តិច (យ៉ាងតិច ៥ តួអក្សរ)។")
        return WAITING_FOR_FEEDBACK_TEXT

    context.user_data["feedback_text"] = feedback_text
    msg = (
        "✅ បានទទួលអត្ថបទ feedback របស់អ្នកហើយ!\n\n"
        "ឥឡូវនេះ អ្នកអាច:\n"
        "• ផ្ញើរូបភាព ១ សន្លឹក ដើម្បីភ្ជាប់ជាមួយ feedback\n"
        "ឬ\n"
        "• វាយ /skip ដើម្បីបញ្ជូន feedback ដោយគ្មានរូបភាព"
    )
    await update.message.reply_text(msg)
    return WAITING_FOR_FEEDBACK_IMAGE


async def feedback_receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await run_security_checks(update, user.id):
        return ConversationHandler.END

    feedback_text = context.user_data.get("feedback_text", "").strip()
    if not feedback_text:
        await update.message.reply_text("រកមិនឃើញអត្ថបទ feedback ទេ។ សូមចាប់ផ្តើមម្តងទៀតដោយវាយ /feedback")
        return ConversationHandler.END

    image_file = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = PIL.Image.open(io.BytesIO(photo_bytes)).convert("RGB")
        image_file = f"feedback_{uuid.uuid4().hex[:8]}.jpg"
        image.save(os.path.join(UPLOAD_DIR, image_file))
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        doc_file = await update.message.document.get_file()
        photo_bytes = await doc_file.download_as_bytearray()
        image = PIL.Image.open(io.BytesIO(photo_bytes)).convert("RGB")
        image_file = f"feedback_doc_{uuid.uuid4().hex[:8]}.jpg"
        image.save(os.path.join(UPLOAD_DIR, image_file))
    else:
        await update.message.reply_text("សូមផ្ញើរូបភាពតែប៉ុណ្ណោះ ឬវាយ /skip ដើម្បីបញ្ចប់។")
        return WAITING_FOR_FEEDBACK_IMAGE

    save_user_feedback(user, feedback_text, image_filename=image_file)
    context.user_data.pop("feedback_text", None)
    await update.message.reply_text(
        "🎉 អរគុណសម្រាប់ feedback!\n"
        "យើងបានរក្សាទុកមតិយោបល់របស់អ្នករួចរាល់ ហើយ admin អាចពិនិត្យបានក្នុង dashboard។"
    )
    return ConversationHandler.END


async def feedback_skip_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await run_security_checks(update, user.id):
        return ConversationHandler.END

    feedback_text = context.user_data.get("feedback_text", "").strip()
    if not feedback_text:
        await update.message.reply_text("រកមិនឃើញអត្ថបទ feedback ទេ។ សូមចាប់ផ្តើមម្តងទៀតដោយវាយ /feedback")
        return ConversationHandler.END

    save_user_feedback(user, feedback_text, image_filename=None)
    context.user_data.pop("feedback_text", None)
    await update.message.reply_text(
        "🎉 អរគុណសម្រាប់ feedback!\n"
        "យើងបានរក្សាទុកមតិយោបល់របស់អ្នករួចរាល់។"
    )
    return ConversationHandler.END


async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("feedback_text", None)
    await update.message.reply_text("Feedback form ត្រូវបានបោះបង់។")
    return ConversationHandler.END


async def admin_feedback_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 អ្នកមិនមានសិទ្ធិចូលប្រើ dashboard នេះទេ។")
        return

    entries = load_feedback_entries()
    if not entries:
        await update.message.reply_text("📭 មិនទាន់មាន feedback ពីអ្នកប្រើប្រាស់នៅឡើយទេ។")
        return

    limit = 10
    if context.args:
        try:
            limit = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass

    selected = list(reversed(entries))[:limit]
    total = len(entries)
    with_images = sum(1 for item in entries if item.get("image_file"))

    header = (
        f"📊 <b>Feedback Dashboard</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total feedback: <b>{total}</b>\n"
        f"With images: <b>{with_images}</b>\n"
        f"Showing latest: <b>{len(selected)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    await update.message.reply_text(header, parse_mode='HTML')

    for idx, item in enumerate(selected, 1):
        username = html.escape(item.get("username", "Unknown"))
        full_name = html.escape(item.get("full_name", "Unknown"))
        feedback_text = html.escape(item.get("feedback_text", ""))
        has_image = "Yes" if item.get("image_file") else "No"
        profile_link = item.get("profile_link", "")
        timestamp = html.escape(item.get("timestamp", "Unknown"))
        image_name = html.escape(item.get("image_file", "-"))

        msg = (
            f"<b>#{idx}</b> | <b>{timestamp}</b>\n"
            f"👤 Name: {full_name}\n"
            f"🔖 Username: @{username}\n"
            f"🆔 User ID: <code>{item.get('user_id')}</code>\n"
            f"🔗 Profile: <a href='{profile_link}'>Open Profile</a>\n"
            f"🖼️ Image: {has_image} ({image_name})\n"
            f"💬 Feedback:\n{feedback_text}"
        )
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)

# --- 🚀 TEACHER-MODE QUIZ SYSTEM (Conversation Handler) ---
WAITING_FOR_CHAPTER = 1
WAITING_FOR_ANSWERS = 2
WAITING_FOR_FEEDBACK_TEXT = 3
WAITING_FOR_FEEDBACK_IMAGE = 4


def save_user_feedback(user, feedback_text, image_filename=None):
    """Persist feedback entries for admin review."""
    full_name = " ".join([part for part in [user.first_name, user.last_name] if part]).strip() or "Unknown"
    entry = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user.id,
        "username": user.username or "Unknown",
        "full_name": full_name,
        "profile_link": f"tg://user?id={user.id}",
        "feedback_text": feedback_text.strip(),
        "image_file": image_filename
    }

    lock_path = FEEDBACK_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        data = []
        if os.path.exists(FEEDBACK_FILE):
            try:
                with open(FEEDBACK_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = []
        data.append(entry)
        with open(FEEDBACK_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def load_feedback_entries():
    lock_path = FEEDBACK_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if not os.path.exists(FEEDBACK_FILE):
            return []
        try:
            with open(FEEDBACK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

async def quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await run_security_checks(update, user.id): return ConversationHandler.END
    
    settings = get_bot_settings()
    plan = user_db.get_user_plan(user.id, user.username or "Unknown")
    
    if not can_use_feature(plan, "generate_quizzes", settings):
        msg = "📝 មុខងារបង្កើតកម្រងសំណួរ (Custom Quizzes) គឺសម្រាប់តែគណនី Premium ប៉ុណ្ណោះ។ សូមទាក់ទង <a href='https://t.me/narong12/'>@admin</a> ដើម្បីដំឡើងគណនី។"
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)
        return ConversationHandler.END

    msg = ("📝 <b>សូមប្រាប់ខ្ញុំអំពីមេរៀន ឬជំពូកដែលអ្នកចង់ធ្វើតេស្ត៖</b>\n"
           "(ឧទាហរណ៍៖ <i>ជំពូកទី១</i> ឬ <i>មេរៀនកម្ដៅ</i>)\n\n"
           "⚠️ វាយ /cancel ដើម្បីបោះបង់ការបង្កើតវិញ្ញាសា។")
    await update.message.reply_text(msg, parse_mode='HTML')
    
    return WAITING_FOR_CHAPTER

async def quiz_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    chapter = update.message.text
    
    if not await run_security_checks(update, user.id, chapter): return ConversationHandler.END
    
    settings = get_bot_settings()
    allowed, count, limit, plan = await check_and_update_limit(user.id, user.username)
    if not allowed:
        await send_limit_reached_message(update, count, limit, plan, settings)
        return ConversationHandler.END

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        
        # Detect which grade the user is asking about
        chapter_lower = chapter.lower()
        grade_text = "ថ្នាក់ទី៧ និងទី៨"  # Default to Grade 7 & 8
        
        if "៩" in chapter or "9" in chapter or "ទី៩" in chapter.lower():
            grade_text = "ថ្នាក់ទី៩"
        elif "៨" in chapter or "8" in chapter or "ទី៨" in chapter.lower():
            grade_text = "ថ្នាក់ទី៨"
        elif "៧" in chapter or "7" in chapter or "ទី៧" in chapter.lower():
            grade_text = "ថ្នាក់ទី៧"
        
        quiz_prompt = (
            f"សូមបង្កើតវិញ្ញាសាប្រឡងរូបវិទ្យា{grade_text} ដោយផ្អែកលើមេរៀន ឬជំពូក៖ «{chapter}»។\n"
            "យោងតាម Knowledge Base របស់អ្នក សូមរៀបចំវិញ្ញាសាតាមទម្រង់ខាងក្រោម (ពិន្ទុសរុប ៥០ ពិន្ទុ)៖\n"
            "១. សំណួរពហុជ្រើសរើស (Multiple Choice) - ចំនួន ៤ សំណួរ (១០ ពិន្ទុ) (មានជម្រើស A B C D)\n"
            "២. លំហាត់ផ្គូផ្គង (Matching) - ផ្គូផ្គងពាក្យ ឬរូបមន្ត ចំនួន ៥ គូ (១០ ពិន្ទុ)\n"
            "៣. សំណួរសួរ-ឆ្លើយ (Short Q&A) - ចំនួន ២ សំណួរខ្លីៗ (១០ ពិន្ទុ)\n"
            "៤. លំហាត់ប្រធានបទ (Exercise) - ចំនួន ១ លំហាត់សម្រាប់គណនា (២០ ពិន្ទុ)\n\n"
            "❌ សំខាន់បំផុត៖ ហាមបង្ហាញចម្លើយ! គ្រាន់តែបង្កើតសំណួរប៉ុណ្ណោះ ដើម្បីឱ្យសិស្សសាកល្បងធ្វើដោយខ្លួនឯងសិន។"
        )
        context.user_data["quiz_chapter"] = chapter
        retrieved = retrieve_relevant_context(chapter)
        chat = get_chat_session(
            user.id, plan, settings, extra_knowledge=retrieved
        )
        await wait_for_rate_limit()
        
        response = chat.send_message(quiz_prompt)
        bot_response = improve_math_font(response.text)
        bot_response = enforce_dataset_symbols(bot_response, retrieved, chapter)
        
        context.user_data['active_quiz'] = bot_response
        save_to_history(user.id, user.username, f"/quiz {chapter}", "Generated Quiz without answers")
        
        final_msg = f"{bot_response}\n\n━━━━━━━━━━━━━━━━━━\n✅ <b>សូមសរសេរចម្លើយរបស់អ្នក ឬថតរូបចម្លើយបញ្ជូនមកកាន់ខ្ញុំដើម្បីឱ្យគ្រូ AI កែ និងដាក់ពិន្ទុឱ្យអ្នក!</b>\n(វាយ /cancel ដើម្បីបោះបង់)"
        await update.message.reply_text(final_msg, parse_mode='HTML')
        
    except Exception as e:
        print(f"❌ QUIZ GEN ERROR: {e}")
        await update.message.reply_text("សុំទោស មានបញ្ហាក្នុងការបង្កើតកម្រងសំណួរនៅពេលនេះ។")
        return ConversationHandler.END
        
    return WAITING_FOR_ANSWERS

async def quiz_grade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    
    if not await run_security_checks(update, user.id): return ConversationHandler.END
    
    active_quiz = context.user_data.get('active_quiz')
    if not active_quiz:
        await update.message.reply_text("រកមិនឃើញវិញ្ញាសាចាស់ទេ។ សូមបង្កើតវិញ្ញាសាថ្មីដោយវាយ /quiz ។")
        return ConversationHandler.END

    settings = get_bot_settings()
    plan = user_db.get_user_plan(user.id, user.username or "Unknown")
    allowed, count, limit, plan = await check_and_update_limit(user.id, user.username)
    
    if not allowed:
        await send_limit_reached_message(update, count, limit, plan, settings)
        return ConversationHandler.END

    await update.message.reply_text("👨‍🏫 កំពុងពិនិត្យ និងកែចម្លើយរបស់អ្នក... សូមរង់ចាំបន្តិច!")
    
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        quiz_ctx = (context.user_data.get("quiz_chapter") or "") + "\n" + (active_quiz or "")[:3000]
        stud = (update.message.text or update.message.caption or "")[:4000]
        retrieved = retrieve_relevant_context(quiz_ctx + "\n" + stud)
        chat = get_chat_session(
            user.id, plan, settings, extra_knowledge=retrieved
        )
        
        grading_prompt = (
            f"នេះគឺជាវិញ្ញាសាដែលអ្នកបានសួរទៅសិស្ស៖\n{active_quiz}\n\n"
            "នេះគឺជាចម្លើយរបស់សិស្ស។ សូមកែ និងដាក់ពិន្ទុ (សរុប ៥០ ពិន្ទុ) ដោយពន្យល់កន្លែងខុស និងបង្ហាញចម្លើយដែលត្រឹមត្រូវពិតប្រាកដជាភាសាខ្មែរ៖"
        )
        
        await wait_for_rate_limit()

        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            image = PIL.Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            
            img_filename = f"img_quiz_{uuid.uuid4().hex[:8]}.jpg"
            image.save(os.path.join(UPLOAD_DIR, img_filename))
            
            response = chat.send_message([image, grading_prompt])
            save_to_history(user.id, user.username, "[QUIZ ANSWER IMAGE]", improve_math_font(response.text), image_filename=img_filename)
        else:
            user_text = update.message.text
            grading_prompt += f"\n\nចម្លើយសិស្ស៖ {user_text}"
            
            response = chat.send_message(grading_prompt)
            save_to_history(user.id, user.username, f"[QUIZ ANSWER TEXT]: {user_text}", improve_math_font(response.text))

        bot_response = improve_math_font(response.text)
        bot_response = enforce_dataset_symbols(bot_response, retrieved, stud)
        await update.message.reply_text(bot_response)

    except Exception as e:
        print(f"❌ QUIZ GRADE ERROR: {e}")
        await update.message.reply_text("សុំទោស មានបញ្ហាក្នុងការកែចម្លើយនៅពេលនេះ។")

    context.user_data.pop('active_quiz', None)
    return ConversationHandler.END

async def quiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('active_quiz', None)
    await update.message.reply_text("🚫 ប្រតិបត្តិការកម្រងសំណួរត្រូវបានលុបចោល។ លែងកត់ត្រាចម្លើយទៀតហើយ។")
    return ConversationHandler.END

# --- 🚀 CONTENT HANDLERS ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_text = update.message.text
    
    if not await run_security_checks(update, user.id, user_text): return
    
    settings = get_bot_settings()
    allowed, count, limit, plan = await check_and_update_limit(user.id, user.username)
    print(f"🚀 USER LOG: {user.username} ({user.id}) | PLAN: {plan} | MSGS: {count}/{limit}")
    
    if not allowed:
        await send_limit_reached_message(update, count, limit, plan, settings)
        return
        
    retrieved = retrieve_relevant_context(user_text)
    chat = get_chat_session(
        user.id, plan, settings, extra_knowledge=retrieved
    )
    
    retry_attempts, retry_base_delay = get_retry_params(settings)
    for attempt in range(retry_attempts):
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
            await wait_for_rate_limit()
            
            response = chat.send_message(user_text)
            bot_response = improve_math_font(response.text)
            bot_response = enforce_dataset_symbols(bot_response, retrieved, user_text)
            
            save_to_history(user.id, user.username, user_text, bot_response)
            if try_auto_learn_from_chat(user_text, bot_response):
                print("🧠 Auto-learn: saved Q/A to Past_Examples in Data_Training.json")
            
            await update.message.reply_text(bot_response)
            break 
            
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e).upper():
                if attempt < retry_attempts - 1:
                    await asyncio.sleep(retry_base_delay * (2 ** attempt))
                else:
                    await update.message.reply_text("❌ ម៉ាស៊ីន AI កំពុងរវល់។ សូមសាកល្បងម្ដងទៀតបន្តិចក្រោយ។")
            else:
                await update.message.reply_text("សុំទោស ខ្ញុំជួបបញ្ហាបច្ចេកទេសបន្តិច។")
                print(f"❌ TEXT ERROR: {e}")
                break

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not await run_security_checks(update, user.id): return
    
    settings = get_bot_settings()
    plan = user_db.get_user_plan(user.id, user.username or "Unknown")
    
    if not can_use_feature(plan, "image_analysis", settings):
        msg = "🔒 មុខងារស្កេនរូបភាព (Vision) គឺសម្រាប់តែគណនី Premium ប៉ុណ្ណោះ។ សូមទាក់ទង <a href='https://t.me/narong12/'>@admin</a> ដើម្បីដំឡើងគណនីរបស់អ្នក។"
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)
        return
    
    caption = update.message.caption or ""
    allowed, count, limit, plan = await check_and_update_limit(user.id, user.username)
    if not allowed:
        await send_limit_reached_message(update, count, limit, plan, settings)
        return
    
    retry_attempts, retry_base_delay = get_retry_params(settings)
    for attempt in range(retry_attempts):
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            image = PIL.Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            
            img_filename = f"img_{uuid.uuid4().hex[:8]}.jpg"
            image.save(os.path.join(UPLOAD_DIR, img_filename))
            
            # 🚀 STEP 1: VISUAL TO LATEX & KHMER TEXT EXTRACTION (HIDDEN FROM USER)
            ocr_prompt = (
                "You are an expert technical transcription AI. Analyze this physics problem image.\n\n"
                "CRITICAL RULES:\n"
                "1. TEXT: Transcribe all Khmer text exactly as written without answering the question.\n"
                "2. MATH: Convert all visible formulas, fractions, and variables into Raw LaTeX.\n"
                "3. GRAPHS & DIAGRAMS: If there is a graph or diagram, describe it mathematically. "
                "Identify axes (e.g., v(m/s) and t(s)), key coordinate points, slopes, and shapes. "
                "Convert relationships into LaTeX equations if possible.\n"
                "4. OUTPUT ONLY the transcribed text and the LaTeX descriptions."
            )
            
            ocr_config = types.GenerateContentConfig(
                system_instruction="You convert visual physics problems into Raw LaTeX descriptions and exact Khmer text.",
                temperature=0.0,
                top_k=1 
            )
            
            await wait_for_rate_limit()
            ocr_response = client.models.generate_content(
                model=get_runtime_model(settings),
                contents=[image, ocr_prompt],
                config=ocr_config
            )
            extracted_text = ocr_response.text.strip()
            
            # 🚀 STEP 2: PHYSICS SOLVING
            analysis_prompt = (
                f"នេះគឺជាទិន្នន័យនៃប្រធានលំហាត់រូបវិទ្យា (រួមមានអត្ថបទ និងទិន្នន័យក្រាប/រូបមន្តជា LaTeX) ដែលបានអានពីរូបភាព៖\n"
                f"« {extracted_text} »\n\n"
                "សូមដោះស្រាយលំហាត់នេះជាភាសាខ្មែរ មួយជំហានម្តងៗ ដោយផ្អែកលើចំណេះដឹងរូបវិទ្យា និង Knowledge Base របស់អ្នក។\n"
                "ប្រសិនបើមានទិន្នន័យក្រាប (Graph) សូមពន្យល់ពីរបៀបដែលអ្នកយកលេខពីក្រាបនោះមកគណនា។\n"
                "⚠️ បម្រាម៖ ដោះស្រាយតែទៅតាមទិន្នន័យខាងលើប៉ុណ្ណោះ។ ហាមយកលំហាត់ផ្សេងមកឆ្លើយជំនួស!"
            )
            if caption: analysis_prompt += f"\nសំណួរបន្ថែមពីសិស្ស៖ {caption}"
            
            rsrc = f"{caption or ''}\n{extracted_text}"[:10000]
            chat = get_chat_session(
                user.id, plan, settings, extra_knowledge=retrieve_relevant_context(rsrc)
            )
            await wait_for_rate_limit()
            
            response = chat.send_message(analysis_prompt)
            bot_response = improve_math_font(response.text)
            bot_response = enforce_dataset_symbols(bot_response, retrieve_relevant_context(rsrc), rsrc)
            
            # 🚀 Send ONLY the clean solved answer
            save_to_history(user.id, user.username, f"[IMAGE] {caption}", bot_response, image_filename=img_filename)
            await update.message.reply_text(bot_response)
            break 
            
        except Exception as e:
            if attempt == retry_attempts - 1:
                await update.message.reply_text("❌ មុខងាររូបភាពកំពុងរវល់ខ្លាំង ឬរូបភាពធំពេក។")
            else:
                await asyncio.sleep(retry_base_delay * (2 ** attempt))

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not await run_security_checks(update, user.id):
        return
    await update.message.reply_text("មិនមានមុខងារនេះទេ")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not await run_security_checks(update, user.id): return
    
    settings = get_bot_settings()
    plan = user_db.get_user_plan(user.id, user.username or "Unknown")
    
    document = update.message.document
    mime_type = document.mime_type
    
    if document.file_size and document.file_size > 20000000:
        await update.message.reply_text("❌ ឯកសារធំជាង 20MB! សូមផ្ញើឯកសារតូចជាងនេះ។")
        return
    
    # --- IF IMAGE SENT AS DOCUMENT ---
    if mime_type and mime_type.startswith('image/'):
        if not can_use_feature(plan, "image_analysis", settings):
            msg = "🔒 មុខងារស្កេនរូបភាព (Vision) គឺសម្រាប់តែគណនី Premium ប៉ុណ្ណោះ។ សូមទាក់ទង <a href='https://t.me/narong12/'>@admin</a> ។"
            await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)
            return
            
        allowed, count, limit, plan = await check_and_update_limit(user.id, user.username)
        if not allowed:
            await send_limit_reached_message(update, count, limit, plan, settings)
            return

        retry_attempts, retry_base_delay = get_retry_params(settings)
        for attempt in range(retry_attempts):
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
                doc_file = await document.get_file()
                photo_bytes = await doc_file.download_as_bytearray()
                image = PIL.Image.open(io.BytesIO(photo_bytes)).convert("RGB")
                
                img_filename = f"img_doc_{uuid.uuid4().hex[:8]}.jpg"
                image.save(os.path.join(UPLOAD_DIR, img_filename))
                
                caption = update.message.caption or ""
                
                # 🚀 STEP 1: VISUAL TO LATEX & KHMER TEXT EXTRACTION (HIDDEN FROM USER)
                ocr_prompt = (
                    "You are an expert technical transcription AI. Analyze this physics problem image.\n\n"
                    "CRITICAL RULES:\n"
                    "1. TEXT: Transcribe all Khmer text exactly as written without answering the question.\n"
                    "2. MATH: Convert all visible formulas, fractions, and variables into Raw LaTeX.\n"
                    "3. GRAPHS & DIAGRAMS: If there is a graph or diagram, describe it mathematically. "
                    "Identify axes (e.g., v(m/s) and t(s)), key coordinate points, slopes, and shapes. "
                    "Convert relationships into LaTeX equations if possible.\n"
                    "4. OUTPUT ONLY the transcribed text and the LaTeX descriptions."
                )
                
                ocr_config = types.GenerateContentConfig(
                    system_instruction="You convert visual physics problems into Raw LaTeX descriptions and exact Khmer text.",
                    temperature=0.0,
                    top_k=1
                )
                
                await wait_for_rate_limit()
                ocr_response = client.models.generate_content(
                    model=get_runtime_model(settings),
                    contents=[image, ocr_prompt],
                    config=ocr_config
                )
                extracted_text = ocr_response.text.strip()
                
                # 🚀 STEP 2: PHYSICS SOLVING
                analysis_prompt = (
                    f"នេះគឺជាទិន្នន័យនៃប្រធានលំហាត់រូបវិទ្យា (រួមមានអត្ថបទ និងទិន្នន័យក្រាប/រូបមន្តជា LaTeX) ដែលបានអានពីរូបភាព៖\n"
                    f"« {extracted_text} »\n\n"
                    "សូមដោះស្រាយលំហាត់នេះជាភាសាខ្មែរ មួយជំហានម្តងៗ ដោយផ្អែកលើចំណេះដឹងរូបវិទ្យា និង Knowledge Base របស់អ្នក។\n"
                    "ប្រសិនបើមានទិន្នន័យក្រាប (Graph) សូមពន្យល់ពីរបៀបដែលអ្នកយកលេខពីក្រាបនោះមកគណនា។\n"
                    "⚠️ បម្រាម៖ ដោះស្រាយតែទៅតាមទិន្នន័យខាងលើប៉ុណ្ណោះ។ ហាមយកលំហាត់ផ្សេងមកឆ្លើយជំនួស!"
                )
                if caption: analysis_prompt += f"\nសំណួរបន្ថែមពីសិស្ស៖ {caption}"
                
                rsrc = f"{caption or ''}\n{extracted_text}"[:10000]
                chat = get_chat_session(
                    user.id, plan, settings, extra_knowledge=retrieve_relevant_context(rsrc)
                )
                await wait_for_rate_limit()
                
                response = chat.send_message(analysis_prompt)
                bot_response = improve_math_font(response.text)
                bot_response = enforce_dataset_symbols(bot_response, retrieve_relevant_context(rsrc), rsrc)
                
                # 🚀 Send ONLY the clean solved answer
                save_to_history(user.id, user.username, f"[FILE IMAGE] {document.file_name}", bot_response, image_filename=img_filename)
                await update.message.reply_text(bot_response)
                return 
            except Exception as e:
                if attempt == retry_attempts - 1:
                    await update.message.reply_text("❌ មុខងាររូបភាពកំពុងរវល់ខ្លាំង។")
                else:
                    await asyncio.sleep(retry_base_delay * (2 ** attempt))
        return

    # --- IF PDF DOCUMENT ---
    elif mime_type == 'application/pdf':
        if not can_use_feature(plan, "pdf_reading", settings):
            msg = "📄 មុខងារអានឯកសារ (PDF) គឺសម្រាប់តែគណនី Premium ប៉ុណ្ណោះ។ សូមទាក់ទង <a href='https://t.me/narong12/'>@admin</a> ។"
            await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)
            return

        allowed, count, limit, plan = await check_and_update_limit(user.id, user.username)
        if not allowed:
            await send_limit_reached_message(update, count, limit, plan, settings)
            return

        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
            doc_file = await document.get_file()
            doc_bytes = await doc_file.download_as_bytearray()
            
            part = types.Part.from_bytes(data=bytes(doc_bytes), mime_type='application/pdf')
            
            # 🚀 STEP 1: PDF TO LATEX & KHMER TEXT EXTRACTION (HIDDEN FROM USER)
            
            ocr_prompt = (
                "You are an expert technical transcription AI. Analyze this PDF document.\n\n"
                "CRITICAL RULES:\n"
                "1. TEXT: Transcribe all Khmer text exactly as written without answering the question.\n"
                "2. MATH: Convert all visible formulas, fractions, and variables into Raw LaTeX.\n"
                "3. GRAPHS & DIAGRAMS: If there is a graph or diagram, describe it mathematically. "
                "Identify axes, key coordinate points, slopes, and shapes. Convert relationships into LaTeX.\n"
                "4. OUTPUT ONLY the transcribed text and the LaTeX descriptions."
            )
            
            ocr_config = types.GenerateContentConfig(
                system_instruction="You convert visual physics problems in PDFs into Raw LaTeX descriptions and exact Khmer text.",
                temperature=0.0,
                top_k=1
            )
            
            await wait_for_rate_limit()
            ocr_response = client.models.generate_content(
                model=get_runtime_model(settings),
                contents=[part, ocr_prompt],
                config=ocr_config
            )
            extracted_text = ocr_response.text.strip()
            
            # 🚀 STEP 2: PHYSICS SOLVING FROM PDF DATA
            analysis_prompt = (
                f"នេះគឺជាទិន្នន័យនៃប្រធានលំហាត់រូបវិទ្យា (រួមមានអត្ថបទ និងទិន្នន័យក្រាប/រូបមន្តជា LaTeX) ដែលបានអានពីឯកសារ PDF៖\n"
                f"« {extracted_text} »\n\n"
                "សូមដោះស្រាយលំហាត់ ឬសង្ខេបចំណុចសំខាន់ៗដែលមាននៅក្នុងនេះជាភាសាខ្មែរ។\n"
                "ប្រសិនបើមានទិន្នន័យក្រាប សូមពន្យល់ពីរបៀបដែលអ្នកប្រើប្រាស់វាក្នុងការដោះស្រាយ។"
            )
            
            rsrc = (update.message.caption or "") + "\n" + extracted_text[:10000]
            chat = get_chat_session(
                user.id, plan, settings, extra_knowledge=retrieve_relevant_context(rsrc)
            )
            await wait_for_rate_limit()
            
            response = chat.send_message(analysis_prompt)
            bot_response = improve_math_font(response.text)
            bot_response = enforce_dataset_symbols(bot_response, retrieve_relevant_context(rsrc), rsrc)
            
            # 🚀 Send ONLY the clean solved answer
            save_to_history(user.id, user.username, f"[PDF DOCUMENT] {document.file_name}", bot_response)
            await update.message.reply_text(bot_response)
            
        except Exception as e:
            print(f"❌ PDF ERROR: {e}")
            import traceback
            print(traceback.format_exc())
            await update.message.reply_text("សុំទោស ឯកសារនេះធំពេក ឬមានបញ្ហាក្នុងការអាន។")
    else:
        await update.message.reply_text("សុំទោស! ខ្ញុំអាចអានបានតែឯកសារប្រភេទ PDF ឬ រូបភាព (Image) ប៉ុណ្ណោះ។")

# --- Main ---
def main():
    dropped = run_history_retention_once(HISTORY_FILE)
    if dropped:
        print(f"🗂️ Chat history retention: removed {dropped} entry/entries outside the retention window.")
    print("🚀 Bot is starting and checking for available models...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    quiz_handler = ConversationHandler(
        entry_points=[CommandHandler("quiz", quiz_start)],
        states={
            WAITING_FOR_CHAPTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_generate)],
            WAITING_FOR_ANSWERS: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, quiz_grade)]
        },
        fallbacks=[CommandHandler("cancel", quiz_cancel)]
    )

    feedback_handler = ConversationHandler(
        entry_points=[CommandHandler("feedback", feedback_start)],
        states={
            WAITING_FOR_FEEDBACK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_receive_text)],
            WAITING_FOR_FEEDBACK_IMAGE: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, feedback_receive_image)
            ]
        },
        fallbacks=[
            CommandHandler("skip", feedback_skip_image),
            CommandHandler("cancel", feedback_cancel)
        ]
    )
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(quiz_handler) 
    app.add_handler(feedback_handler)
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("feedbacks", admin_feedback_dashboard))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    app.run_polling()

if __name__ == '__main__':
    main()