import streamlit as st
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google import genai
from google.genai import types
import datetime
import pandas as pd
import json
from PIL import Image
import re
import time
import os

# ==========================================
# 1. 환경 설정 및 모델 고정
# ==========================================
st.set_page_config(page_title="Google Workout", page_icon="💪", layout="wide")
SHEET_NAME = "운동일지_DB"

# [절대 준수] 매니저님 지정 모델 리스트
MODEL_CANDIDATES = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview", 
    "gemini-2.5-flash",
]

# 클라우드 Secrets 인증 (이제 코드에 키를 적지 않습니다)
try:
    if "GEMINI_API_KEY" in st.secrets:
        GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    else:
        # 로컬 테스트용 (파일이 있을 때만 작동)
        # 주의: GitHub에 올릴 때는 절대 여기에 키를 적지 마세요.
        BASE_DIR = r"C:\Users\USER\Desktop"
        json_path = os.path.join(BASE_DIR, "service_account.json")
        
        if os.path.exists(json_path):
             # 로컬에서만 쓰는 비밀 파일에서 키를 읽어오거나, 환경변수 사용 권장
             # 여기서는 안전을 위해 하드코딩 제거함
             st.error("로컬 테스트 시 Secrets 설정이 필요합니다.")
             st.stop()
        else:
            st.error("Secrets 설정을 찾을 수 없습니다.")
            st.stop()

    client_sheet = gspread.authorize(creds)
    spreadsheet = client_sheet.open(SHEET_NAME)
    client_ai = genai.Client(api_key=GEMINI_API_KEY)

except Exception as e:
    st.error(f"❌ 설정 오류: {e}")
    st.stop()

# ==========================================
# 2. JSON 가이드
# ==========================================
JSON_GUIDE = """
**[작동 규칙]**
1. 단순 대화: { "type": "chat", "response": "..." }
2. 식단 기록: { "type": "diet", "data": { "breakfast": "...", "lunch": "...", "snack": "...", "dinner": "...", "supplement": "...", "total_input": "...", "score": "..." }, "feedback": "..." }
3. 운동 기록: 
   - 근력 운동: sets=세트수, weight=무게, reps=횟수, volume=볼륨
   - **유산소 운동: sets='시간(분)', weight='속도/강도/인클라인'** (reps, onerm 등은 비워둠)
   { "type": "workout", "details": [ { "target_sheet": "...", "exercise": "...", "sets": "...", "weight": "...", "reps": "...", "onerm": "...", "volume": "...", "note": "..." } ], "summary": { "parts": "...", "main_exercise": "...", "total_volume": "...", "feedback": "..." } }
"""

# ==========================================
# 3. 비즈니스 로직
# ==========================================
def get_user_profile():
    try:
        return "\n".join([f"- {row[0]}: {row[1]}" for row in spreadsheet.worksheet("프로필").get_all_values() if len(row) >= 2])
    except: return "프로필 없음"

def calculate_past_workout_stats():
    """근력 운동 시트만 계산 (유산소 제외)"""
    try:
        sheet_list = ["등", "가슴", "하체", "어깨", "이두", "삼두", "복근", "기타"]
        total_updated = 0
        
        for sheet_name in sheet_list:
            try:
                ws = spreadsheet.worksheet(sheet_name)
                rows = ws.get_all_values()
                if len(rows) < 2: continue
                
                header = rows[0]
                try:
                    idx_w = next(i for i, h in enumerate(header) if "무게" in h)
                    idx_r = next(i for i, h in enumerate(header) if "횟수" in h)
                    idx_1rm = next(i for i, h in enumerate(header) if "1RM" in h)
                    idx_vol = next(i for i, h in enumerate(header) if "볼륨" in h)
                except: continue

                for i, row in enumerate(rows[1:], start=2):
                    current_vol = row[idx_vol] if len(row) > idx_vol else ""
                    
                    if not current_vol: 
                        try:
                            w_str, r_str = str(row[idx_w]), str(row[idx_r])
                            weights = [float(x) for x in re.findall(r"[\d\.]+", w_str)]
                            reps = [float(x) for x in re.findall(r"[\d\.]+", r_str)]

                            if weights and reps:
                                vol_val = 0
                                if len(weights) == len(reps): vol_val = sum(w*r for w, r in zip(weights, reps))
                                else:
                                    avg_w = sum(weights)/len(weights)
                                    avg_r = sum(reps)/len(reps)
                                    vol_val = avg_w * avg_r * max(len(weights), len(reps))
                                
                                max_w = max(weights)
                                max_r = reps[weights.index(max_w)] if len(reps) > weights.index(max_w) else reps[0]
                                onerm_val = max_w * (1 + max_r/30)

                                ws.update_cell(i, idx_1rm + 1, int(onerm_val))
                                ws.update_cell(i, idx_vol + 1, int(vol_val))
                                total_updated += 1
                                time.sleep(0.5)
                        except: continue
            except: continue

        return f"근력 운동 {total_updated}건 계산 완료 (유산소 제외)"
    except Exception as e: return f"오류: {e}"

def fill_past_diet_blanks(profile_txt):
    """식단 빈칸 채우기"""
    try:
        ws = spreadsheet.worksheet("식단")
        rows = ws.get_all_values()
        try:
            idx_total = next(i for i, h in enumerate(rows[0]) if "Total" in h)
            idx_score = next(i for i, h in enumerate(rows[0]) if "Score" in h)
        except: return "식단 시트 헤더를 찾을 수 없습니다."

        updates_needed = []
        for i, row in enumerate(rows[1:], start=2):
            is_empty = (len(row) <= idx_total) or (not row[idx_total])
            has_content = any(row[j] for j in range(1, idx_total) if len(row) > j and row[j])
            if is_empty and has_content:
                # 데이터가 너무 길어지면 에러가 날 수 있으므로 최근 20개까지만 처리하거나 나눠서 처리 권장
                row_data = ", ".join([f"{rows[0][j]}:{row[j]}" for j in range(1, idx_total) if len(row) > j and row[j]])
                updates_needed.append(f"Row {i}: {row_data}")
        
        if not updates_needed: return "채울 빈칸이 없습니다."

        prompt = f"영양사로서 다음 식단의 Total Input, Score를 계산해 JSON List로 반환.\n프로필: {profile_txt}\n데이터: {chr(10).join(updates_needed)}\nOutput format: [{{'row': 2, 'total_input': '...', 'score': 80}}]"
        
        result = None
        last_error = ""
        
        for model in MODEL_CANDIDATES:
            try:
                response = client_ai.models.generate_content(
                    model=model, 
                    contents=prompt, 
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                result = json.loads(response.text)
                break
            except Exception as e: 
                last_error = str(e)
                continue
            
        if not result: 
            return f"AI 응답 실패. (마지막 에러: {last_error})"

        cnt = 0
        for item in result:
            ws.update_cell(item['row'], idx_total + 1, item['total_input'])
            ws.update_cell(item['row'], idx_score + 1, item['score'])
            cnt += 1
            time.sleep(0.5)
        return f"{cnt}건 식단 업데이트 완료"
    except Exception as e: return f"오류: {e}"

# ==========================================
# 4. 메인 UI
# ==========================================
st.title("Google Workout")

with st.sidebar:
    st.header("Workout Log") 
    
    if st.button("🏋️ 근력 운동 계산"):
        with st.spinner("계산 중..."): st.success(calculate_past_workout_stats())
        
    if st.button("🥗 식단 빈칸 계산"):
        with st.spinner("AI 분석 중... (시간이 좀 걸립니다)"): 
            msg = fill_past_diet_blanks(get_user_profile())
            if "실패" in msg:
                st.error(msg)
            else:
                st.success(msg)

if "messages" not in st.session_state: st.session_state.messages = []
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]): st.markdown(msg["content"])

uploaded_file = st.file_uploader("📸 사진 분석", type=['png', 'jpg', 'jpeg'])

if prompt := st.chat_input("기록할 내용을 입력하세요..."):
    if uploaded_file:
        img = Image.open(uploaded_file)
        st.chat_message("user").image(img, width=200)
        st.session_state.messages.append({"role": "user", "content": "[사진]"})
    else:
        st.chat_message("user").markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

    with st.spinner("AI 처리 중..."):
        profile_txt = get_user_profile()
        contents = ["Profile:\n" + profile_txt + "\n\n" + JSON_GUIDE + "\nInput: " + prompt]
        if uploaded_file: contents.append(img)

        result = None
        for model in MODEL_CANDIDATES:
            try:
                response = client_ai.models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(response_mime_type="application/json"))
                result = json.loads(response.text)
                break
            except: continue

        reply = ""
        if not result: reply = "❌ 응답 실패 (새로운 API 키로 교체했는지 확인해주세요)"
        else:
            try:
                if result.get('type') == 'chat': reply = result.get('response')
                elif result.get('type') == 'diet':
                    ws = spreadsheet.worksheet("식단")
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    d = result['data']
                    ws.append_row([today, d.get('breakfast'), d.get('lunch'), d.get('snack'), d.get('dinner'), d.get('supplement'), d.get('total_input'), d.get('score')])
                    reply = f"🥗 기록 완료: {d.get('total_input')}"
                elif result.get('type') == 'workout':
                    cnt = 0
                    for d in result.get('details', []):
                        ws = spreadsheet.worksheet(d.get('target_sheet'))
                        today = datetime.datetime.now().strftime("%Y-%m-%d")
                        
                        # 유산소 vs 근력 운동 분기 처리
                        if d.get('target_sheet') == "유산소":
                            ws.append_row([today, d.get('exercise'), d.get('sets'), d.get('weight'), d.get('note')])
                        else:
                            ws.append_row([today, d.get('exercise'), d.get('sets'), d.get('weight'), d.get('reps'), d.get('onerm'), d.get('volume'), d.get('note')])
                        cnt += 1
                    reply = f"🏋️ {cnt}건 기록 완료."
            except Exception as e: reply = f"저장 중 오류: {e}"

        st.chat_message("assistant").markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})
