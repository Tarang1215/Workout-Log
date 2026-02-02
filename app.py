import streamlit as st
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google import genai
from google.genai import types
import datetime
from datetime import timedelta
import pandas as pd
import os
import json
from PIL import Image

# ==========================================
# 1. 환경 설정 (클라우드 전용)
# ==========================================
# ⚠️ 내 컴퓨터 경로(BASE_DIR)는 클라우드에서 필요 없으므로 삭제했습니다.
# 대신 Streamlit Secrets에서 정보를 가져옵니다.

SHEET_NAME = "운동일지_DB"

# Secrets에서 API 키 가져오기
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
except Exception as e:
    st.error("❌ Secrets에서 GEMINI_API_KEY를 찾을 수 없습니다.")
    st.stop()

# 모델 설정 (요청하신 그대로 유지)
MODEL_CANDIDATES = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview", 
    "gemini-2.5-flash"
]

# ==========================================
# 2. 프롬프트 가이드 (기존 동일)
# ==========================================
JSON_GUIDE_PROMPT = """
**[작동 규칙]**
User의 입력(텍스트 또는 이미지)을 분석하여 **[단순 대화]**인지 **[기록 요청]**인지 판단하십시오.

---
**Case 1. 단순 대화 (기록 X)**
{ "type": "chat", "response": "..." }

---
**Case 2. 식단 기록 (기록 O)**
이미지 입력 시, 음식의 종류와 양을 추정하고 프로필의 [선호 브랜드]를 우선 적용하십시오.
{
    "type": "diet",
    "data": { "breakfast": "...", "lunch": "...", "snack": "...", "dinner": "...", "supplement": "...", "total_input": "...", "score": "..." },
    "feedback": "..."
}

---
**Case 3. 운동 기록 (기록 O)**
운동 기구 화면이나 루틴 메모 사진일 경우 텍스트로 추출하여 정리하십시오.
{
    "type": "workout",
    "details": [ { "target_sheet": "...", "exercise": "...", "sets": "...", "weight": "...", "reps": "...", "volume": "...", "note": "..." } ],
    "summary": { "parts": "...", "main_exercise": "...", "sub_exercises": "...", "total_volume": "...", "feedback": "..." }
}
"""

# ==========================================
# 3. 연결 및 함수 (클라우드 인증 방식 적용)
# ==========================================
st.set_page_config(page_title="My Workout Analyst", page_icon="📈", layout="wide")

# 사이드바 (기능 모음)
with st.sidebar:
    st.header("Workout Log")
    st.write("made by & for June")
    
# 구글 시트 인증 (Secrets 사용)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
try:
    # 🔴 [변경] 로컬 파일 대신 Secrets에 있는 정보를 딕셔너리로 변환하여 사용
    credentials_dict = dict(st.secrets["gcp_service_account"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
    client_sheet = gspread.authorize(creds)
    spreadsheet = client_sheet.open(SHEET_NAME)
except Exception as e:
    st.error(f"❌ 시트 연결 실패 (Secrets 설정을 확인하세요): {e}")
    st.stop()

# Gemini 인증
try:
    client_ai = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    st.error(f"❌ Gemini 연결 실패: {e}")
    st.stop()

# --- 데이터 핸들링 함수들 (기존 동일) ---
def get_user_profile():
    try:
        ws = spreadsheet.worksheet("프로필")
        data = ws.get_all_values()
        return "\n".join([f"- {row[0]}: {row[1]}" for row in data if len(row) >= 2])
    except: return "프로필 정보 없음"

def load_chat_history():
    try:
        ws = spreadsheet.worksheet("채팅기록")
        data = ws.get_all_values()[1:] 
        recent_data = data[-20:] if len(data) > 20 else data # 최근 20개만 로드
        history = []
        for row in recent_data:
            if len(row) >= 3: history.append({"role": row[1], "content": row[2]})
        return history
    except: return []

def save_chat_message(role, content):
    try:
        ws = spreadsheet.worksheet("채팅기록")
        ws.append_row([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), role, str(content)[:1000]])
    except: pass

def get_weekly_data():
    """지난 7일간의 통합로그와 식단 데이터를 가져옵니다."""
    try:
        # 통합로그 가져오기
        log_ws = spreadsheet.worksheet("통합로그")
        log_df = pd.DataFrame(log_ws.get_all_records())
        
        # 식단 가져오기
        diet_ws = spreadsheet.worksheet("식단")
        diet_df = pd.DataFrame(diet_ws.get_all_records())

        # 간단히 텍스트로 변환
        return f"[최근 운동 로그]:\n{log_df.tail(7).to_string()}\n\n[최근 식단 로그]:\n{diet_df.tail(7).to_string()}"
    except Exception as e:
        return f"데이터 로드 실패: {e}"

# --- 기록 업데이트 함수들 (기존 동일) ---
def update_diet_sheet(date_str, data):
    try:
        ws = spreadsheet.worksheet("식단")
        col_map = { 2: data.get('breakfast'), 3: data.get('lunch'), 4: data.get('snack'), 5: data.get('dinner'), 6: data.get('supplement'), 7: data.get('total_input'), 8: data.get('score') }
        cell = ws.find(date_str)
        if cell:
            for col, val in col_map.items(): 
                if val: ws.update_cell(cell.row, col, val)
            return "식단 업데이트"
        else:
            ws.append_row([date_str, data.get('breakfast'), data.get('lunch'), data.get('snack'), data.get('dinner'), data.get('supplement'), data.get('total_input'), data.get('score')])
            return "식단 신규"
    except: return "식단 에러"

def append_workout_detail(date_str, detail_data):
    try:
        ws = spreadsheet.worksheet(detail_data.get('target_sheet'))
        ws.append_row([date_str, detail_data.get('exercise'), detail_data.get('sets'), detail_data.get('weight'), detail_data.get('reps'), detail_data.get('onerm'), detail_data.get('volume'), detail_data.get('note')])
        return True
    except: return False

def update_summary_log(date_str, summary_data):
    try:
        ws = spreadsheet.worksheet("통합로그")
        row_vals = [summary_data.get('parts'), summary_data.get('main_exercise'), summary_data.get('sub_exercises'), summary_data.get('total_volume'), summary_data.get('feedback')]
        cell = ws.find(date_str)
        if cell:
            for i, val in enumerate(row_vals, start=2): ws.update_cell(cell.row, i, val)
            return "통합로그 업데이트"
        else:
            ws.append_row([date_str] + row_vals)
            return "통합로그 신규"
    except: return "통합로그 에러"

# ==========================================
# 4. 메인 UI 및 로직 (기존 동일)
# ==========================================
st.title("Google Workout")

# [기능 3] 주간 리포트 버튼 (사이드바)
if st.sidebar.button("📅 주간 전략 리포트 생성"):
    with st.spinner("지난 7일간의 데이터를 분석 중입니다..."):
        weekly_data = get_weekly_data()
        user_profile = get_user_profile()
        
        report_prompt = f"""
        당신은 펀드매니저의 헬스 전략가입니다.
        아래 [지난 7일간 데이터]와 [프로필]을 분석하여 '주간 전략 보고서'를 작성하세요.
        
        [프로필]: {user_profile}
        [데이터]: {weekly_data}
        
        **포함할 내용:**
        1. **성과 요약:** 이번 주 운동 볼륨 추세, 식단 점수 평가 (상승/하락).
        2. **약점 분석:** 부족했던 부위나 식단의 문제점 (단백질 부족 등).
        3. **다음 주 전략:** 구체적인 운동/식단 목표 제시 (예: "다음 주는 하체 볼륨 10% 증량 필요").
        
        전문적이고 통찰력 있게 작성하세요.
        """
        
        try:
            response = client_ai.models.generate_content(model="gemini-3-pro-preview", contents=report_prompt)
            st.chat_message("assistant").markdown(f"## 📊 주간 전략 리포트\n{response.text}")
            save_chat_message("assistant", f"[주간 리포트 생성함]\n{response.text}")
        except Exception as e:
            st.error(f"리포트 생성 실패: {e}")

# 채팅 기록 로드
if "messages" not in st.session_state:
    st.session_state.messages = load_chat_history()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# [기능 1] 이미지 업로드 (채팅창 위)
uploaded_file = st.file_uploader("📸 사진 분석 (식단/운동기록/인바디)", type=['png', 'jpg', 'jpeg'])

# 입력창
if prompt := st.chat_input("메시지 입력 (또는 사진 업로드 후 입력)"):
    
    # 이미지 처리
    image_part = None
    if uploaded_file:
        image = Image.open(uploaded_file)
        image_part = image
        st.chat_message("user").image(image, caption="이미지 업로드됨", width=200)
        st.chat_message("user").markdown(prompt)
        # 이미지 업로드했다는 텍스트만 기록
        st.session_state.messages.append({"role": "user", "content": f"[사진 업로드] {prompt}"})
        save_chat_message("user", f"[사진] {prompt}")
    else:
        st.chat_message("user").markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})
        save_chat_message("user", prompt)

    with st.spinner("AI가 분석 중..."):
        user_profile = get_user_profile()
        
        # 프롬프트 조립
        PERSONALITY_PROMPT = f"""
당신은 26세 남성 펀드매니저(User)의 헬스 AI입니다.
User의 입력(텍스트/이미지)을 분석하여 적절한 JSON을 생성하십시오.
이미지가 있다면 시각 정보를 정밀하게 분석하여 데이터화하십시오.

⚠️ **[User 프로필 및 절대 제약사항]**
{user_profile}
"""
        full_text_prompt = PERSONALITY_PROMPT + "\n" + JSON_GUIDE_PROMPT + f"\nInput: {prompt}\nOutput JSON Only."
        
        # 콘텐츠 구성 (이미지 유무에 따라)
        contents = [full_text_prompt]
        if image_part:
            contents.append(image_part)
        
        result = None
        used_model = None

        for model_name in MODEL_CANDIDATES:
            try:
                response = client_ai.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                result = json.loads(response.text)
                used_model = model_name
                break 
            except Exception as e:
                continue 

        if result is None:
            st.error("AI 응답 실패. (이미지가 너무 크거나 API 문제일 수 있습니다)")
            st.stop()

        # 결과 처리 (분기)
        bot_reply = ""
        
        if result.get('type') == 'chat':
            bot_reply = result.get('response')
        
        elif result.get('type') == 'diet':
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            status = update_diet_sheet(today, result['data'])
            bot_reply = f"📝 **{status}**\n\n{result['feedback']}"

        elif result.get('type') == 'workout':
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            logs = []
            details = result.get('details', [])
            success_count = 0
            for item in details:
                if append_workout_detail(today, item): success_count += 1
            if result.get('summary'):
                status = update_summary_log(today, result['summary'])
                logs.append(f"📊 **{status}**")
            bot_reply = f"🏋️ **운동 {success_count}건** / {' '.join(logs)}\n\n💡 {result.get('summary', {}).get('feedback')}"

        with st.chat_message("assistant"):
            st.markdown(bot_reply)
        st.session_state.messages.append({"role": "assistant", "content": bot_reply})

        save_chat_message("assistant", bot_reply)
