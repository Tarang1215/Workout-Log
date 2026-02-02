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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ==========================================
# 1. 환경 설정 및 루틴
# ==========================================
st.set_page_config(page_title="Google Workout", page_icon="💪", layout="wide")
SHEET_NAME = "운동일지_DB"

USER_ROUTINE = """
**[매니저님 루틴]**
- 화: 가슴
- 수: 등
- 목: 어깨
- 금: 휴식 (또는 보충)
- 토: 하체
- 일: 팔, 복근, 인터벌러닝
- 월: 휴식
"""

MODEL_CANDIDATES = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview", 
    "gemini-2.5-flash",
]

# [인증 처리]
try:
    if "GEMINI_API_KEY" in st.secrets:
        GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
        
        # 이메일 설정 (없으면 None)
        GMAIL_ID = st.secrets.get("GMAIL_ID")
        GMAIL_PW = st.secrets.get("GMAIL_APP_PW")
    else:
        st.error("❌ Secrets 설정이 필요합니다.")
        st.stop()

    client_sheet = gspread.authorize(creds)
    spreadsheet = client_sheet.open(SHEET_NAME)
    client_ai = genai.Client(api_key=GEMINI_API_KEY)

except Exception as e:
    st.error(f"❌ 설정 오류: {e}")
    st.stop()

# ==========================================
# 2. 프롬프트 가이드
# ==========================================
SCORING_RULES = """
**[User 스펙: 183cm/82kg/골격근41kg, 커팅중]**
1. **단백질:** 120g 미만 감점. (목표: 체중x1.5~2.0)
2. **운동/식단:** 운동한 날 탄수화물은 OK. 휴식일 고탄수는 감점.
3. **포맷:** 음식은 '+'로 연결. Total Input은 "C:.. P:.. F:.. (비율)"
"""

JSON_GUIDE = """
**[작동 규칙]**
1. 식단: { "type": "diet", "data": { "breakfast": "...", "lunch": "...", "snack": "...", "dinner": "...", "total_input": "...", "score": "...", "comment": "..." } }
2. 운동: 
   - 세트별 무게 다르면 "20, 40, 60" (콤마 구분).
   - 유산소는 sets=분, weight=강도.
   { "type": "workout", "details": [ { "target_sheet": "...", "exercise": "...", "sets": "...", "weight": "...", "reps": "...", "note": "..." } ] }
"""

# ==========================================
# 3. 핵심 함수들 (생략 없음)
# ==========================================
def get_user_profile():
    try:
        return "\n".join([f"- {row[0]}: {row[1]}" for row in spreadsheet.worksheet("프로필").get_all_values() if len(row) >= 2])
    except: return "프로필 없음"

def get_workout_volume_dict():
    try:
        ws = spreadsheet.worksheet("통합로그")
        rows = ws.get_all_values()
        vol_dict = {}
        for row in rows[1:]:
            if len(row) > 4:
                vol_dict[row[0]] = f"{row[1]} / {row[4]}kg"
        return vol_dict
    except: return {}

# [기능 1] 운동 계산 및 코멘트 (유산소/복근 포함)
def calculate_past_workout_stats():
    try:
        sheet_list = ["등", "가슴", "하체", "어깨", "이두", "삼두", "복근", "기타", "유산소"]
        total_updated = 0
        
        for sheet_name in sheet_list:
            try:
                ws = spreadsheet.worksheet(sheet_name)
                rows = ws.get_all_values()
                if len(rows) < 2: continue
                
                header = rows[0]
                
                # 인덱스 찾기
                if sheet_name == "유산소":
                    try:
                        idx_time = next(i for i, h in enumerate(header) if "시간" in h or "세트" in h)
                        idx_intensity = next(i for i, h in enumerate(header) if "속도" in h or "강도" in h or "무게" in h)
                        idx_note = next(i for i, h in enumerate(header) if "비고" in h)
                    except: continue
                else:
                    try:
                        idx_set = next(i for i, h in enumerate(header) if "세트" in h)
                        idx_w = next(i for i, h in enumerate(header) if "무게" in h)
                        idx_r = next(i for i, h in enumerate(header) if "횟수" in h)
                        idx_1rm = next(i for i, h in enumerate(header) if "1RM" in h)
                        idx_vol = next(i for i, h in enumerate(header) if "볼륨" in h)
                        idx_note = next(i for i, h in enumerate(header) if "비고" in h)
                    except: continue

                for i, row in enumerate(rows[1:], start=2):
                    current_note = row[idx_note] if len(row) > idx_note else ""
                    
                    # A. 유산소 처리 (코멘트만)
                    if sheet_name == "유산소":
                        time_str = str(row[idx_time]).strip()
                        int_str = str(row[idx_intensity]).strip()
                        if not current_note and (time_str or int_str):
                            try:
                                prompt = f"헬스 코치로서 유산소 피드백 1줄(존댓말). 종목:{row[1]}, 시간:{time_str}, 강도:{int_str}. User: 82kg 상급자."
                                response = client_ai.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
                                ws.update_cell(i, idx_note + 1, response.text.strip())
                                total_updated += 1
                                time.sleep(1)
                            except: pass

                    # B. 근력 처리 (계산 + 코멘트)
                    else:
                        sets_str = str(row[idx_set]).strip()
                        w_str = str(row[idx_w]).strip()
                        r_str = str(row[idx_r]).strip()
                        current_vol = row[idx_vol] if len(row) > idx_vol else ""

                        # 볼륨/1RM 계산
                        if not current_vol and w_str and r_str:
                            try:
                                weights = [float(x) for x in re.findall(r"[\d\.]+", w_str)]
                                reps = [float(x) for x in re.findall(r"[\d\.]+", r_str)]
                                sets_val = float(re.findall(r"[\d\.]+", sets_str)[0]) if re.findall(r"[\d\.]+", sets_str) else 1.0

                                vol_val = 0
                                onerm_val = 0
                                
                                if len(weights) > 1:
                                    if len(reps) == len(weights): vol_val = sum(w * r for w, r in zip(weights, reps))
                                    else: 
                                        r_val = reps[0] if reps else 0
                                        vol_val = sum(w * r_val for w in weights)
                                    onerm_val = max(weights) * (1 + (reps[weights.index(max(weights))] if len(reps) > weights.index(max(weights)) else 0)/30)
                                else:
                                    w_val = weights[0]
                                    if len(reps) > 1:
                                        vol_val = w_val * sum(reps)
                                        onerm_val = w_val * (1 + reps[0]/30)
                                    else:
                                        r_val = reps[0] if reps else 0
                                        vol_val = w_val * r_val * sets_val
                                        onerm_val = w_val * (1 + r_val/30)

                                ws.update_cell(i, idx_1rm + 1, int(onerm_val))
                                ws.update_cell(i, idx_vol + 1, int(vol_val))
                            except: pass

                        # AI 코멘트 (비고 비어있으면)
                        if not current_note and (w_str or r_str or sets_str):
                            try:
                                prompt = f"헬스 코치로서 피드백 1줄(존댓말). 종목:{row[1]}, 세트:{sets_str}, 무게:{w_str}, 횟수:{r_str}."
                                response = client_ai.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
                                ws.update_cell(i, idx_note + 1, response.text.strip())
                                total_updated += 1
                                time.sleep(1)
                            except: pass
            except: continue
        return f"총 {total_updated}건 계산 및 코멘트 작성 완료"
    except Exception as e: return f"오류: {e}"

# [기능 2] 식단 빈칸 채우기
def fill_past_diet_blanks(profile_txt):
    try:
        ws = spreadsheet.worksheet("식단")
        rows = ws.get_all_values()
        try:
            idx_total = next(i for i, h in enumerate(rows[0]) if "Total" in h)
            idx_score = next(i for i, h in enumerate(rows[0]) if "Score" in h)
            idx_comment = 8 
        except: return "식단 헤더 확인 필요"

        workout_history = get_workout_volume_dict()
        updates_needed = []
        
        for i, row in enumerate(rows[1:], start=2):
            is_empty = (len(row) <= idx_total) or (not row[idx_total])
            has_content = any(row[j] for j in range(1, idx_total) if len(row) > j and row[j])
            
            if is_empty and has_content:
                date = row[0]
                workout_info = workout_history.get(date, "휴식")
                row_data = ", ".join([f"{rows[0][j]}:{row[j]}" for j in range(1, idx_total) if len(row) > j and row[j]])
                updates_needed.append(f"Row {i} [{date}]: 식단({row_data}) / 운동({workout_info})")
        
        if not updates_needed: return "채울 빈칸이 없습니다."

        prompt = f"""
        영양사로서 식단을 분석하세요.
        [프로필]: {profile_txt}
        {SCORING_RULES}
        [데이터]:
        {chr(10).join(updates_needed)}
        Output format (JSON List):
        [ {{"row": 2, "total_input": "C:.. P:.. F:..", "score": 85, "comment": ".."}}, ... ]
        """
        
        result = None
        for model in MODEL_CANDIDATES:
            try:
                response = client_ai.models.generate_content(model=model, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
                result = json.loads(response.text)
                break
            except: continue
        
        if not result: return "AI 응답 실패"

        cnt = 0
        for item in result:
            ws.update_cell(item['row'], idx_total + 1, item['total_input'])
            ws.update_cell(item['row'], idx_score + 1, item['score'])
            ws.update_cell(item['row'], idx_comment + 1, item['comment'])
            cnt += 1
            time.sleep(0.5)
        return f"{cnt}건 식단 업데이트 완료"
    except Exception as e: return f"오류: {e}"

# [기능 3] 통합로그 취합 (오늘 운동)
def update_daily_summary():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    sheet_list = ["등", "가슴", "하체", "어깨", "이두", "삼두", "복근", "기타", "유산소"]
    
    total_vol = 0
    main_parts = []
    main_exercises = []
    
    try:
        for sheet in sheet_list:
            ws = spreadsheet.worksheet(sheet)
            rows = ws.get_all_values()
            today_rows = [r for r in rows[1:] if r[0] == today]
            
            if today_rows:
                main_parts.append(sheet)
                if not main_exercises: main_exercises.append(today_rows[0][1])
                
                # 볼륨 합산 (유산소 제외)
                if sheet != "유산소":
                    try:
                        idx_vol = next(i for i, h in enumerate(rows[0]) if "볼륨" in h)
                        for r in today_rows:
                            if len(r) > idx_vol and r[idx_vol]:
                                total_vol += int(re.sub(r'[^0-9]', '', str(r[idx_vol])))
                    except: pass
        
        if not main_parts: return "오늘 기록된 운동이 없습니다."

        summ_ws = spreadsheet.worksheet("통합로그")
        parts_str = ", ".join(main_parts)
        main_ex_str = main_exercises[0] if main_exercises else ""
        sub_ex_str = f"{len(main_parts)}개 부위 수행"
        
        cell = summ_ws.find(today)
        row_data = [today, parts_str, main_ex_str, sub_ex_str, total_vol, ""]
        
        if cell:
            for i, val in enumerate(row_data):
                summ_ws.update_cell(cell.row, i+1, val)
            return f"업데이트 완료: {parts_str}"
        else:
            summ_ws.append_row(row_data)
            return f"신규 등록 완료: {parts_str}"
    except Exception as e: return f"실패: {e}"

# [기능 4] 주간 리포트 이메일
def generate_and_send_report():
    if not GMAIL_ID or not GMAIL_PW: return "❌ 이메일 설정이 없습니다."
    
    try:
        diet_ws = spreadsheet.worksheet("식단")
        log_ws = spreadsheet.worksheet("통합로그")
        diet_data = diet_ws.get_all_values()[-7:]
        log_data = log_ws.get_all_values()[-7:]
        
        prompt = f"""
        트레이너로서 주간 보고서를 작성하세요.
        [프로필]: {get_user_profile()}
        [루틴]: {USER_ROUTINE}
        [지난주 식단]: {diet_data}
        [지난주 운동]: {log_data}
        """
        response = client_ai.models.generate_content(model="gemini-3-pro-preview", contents=prompt)
        report_text = response.text

        msg = MIMEMultipart()
        msg['From'] = GMAIL_ID
        msg['To'] = GMAIL_ID
        msg['Subject'] = f"[{datetime.datetime.now().strftime('%Y-%m-%d')}] 주간 운동 리포트"
        msg.attach(MIMEText(report_text, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_ID, GMAIL_PW)
        server.sendmail(GMAIL_ID, GMAIL_ID, msg.as_string())
        server.quit()
        return "📧 이메일 발송 성공!"
    except Exception as e: return f"전송 실패: {e}"

# ==========================================
# 4. 메인 UI 및 채팅 로직
# ==========================================
st.title("Google Workout")

with st.sidebar:
    st.header("Workout Log")
    st.markdown(USER_ROUTINE)
    
    if st.button("🏋️ 운동 계산 & 코멘트"):
        with st.spinner("계산 중..."): st.success(calculate_past_workout_stats())

    if st.button("🥗 식단 빈칸 계산"):
        with st.spinner("분석 중..."): st.success(fill_past_diet_blanks(get_user_profile()))

    if st.button("🔄 통합로그 취합 (오늘)"):
        with st.spinner("취합 중..."): st.success(update_daily_summary())

    if st.button("📧 주간 리포트 발송"):
        with st.spinner("작성 중..."): st.success(generate_and_send_report())

if "messages" not in st.session_state: st.session_state.messages = []
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]): st.markdown(msg["content"])

uploaded_file = st.file_uploader("📸 사진 분석", type=['png', 'jpg', 'jpeg'])

if prompt := st.chat_input("입력하세요..."):
    # 유저 입력 UI 표시
    if uploaded_file:
        img = Image.open(uploaded_file)
        st.chat_message("user").image(img, width=200)
        st.session_state.messages.append({"role": "user", "content": "[사진]"})
    else:
        st.chat_message("user").markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

    with st.spinner("AI 처리 중..."):
        profile_txt = get_user_profile()
        contents = ["Profile:\n" + profile_txt + "\n\n" + SCORING_RULES + "\n" + JSON_GUIDE + "\nInput: " + prompt]
        if uploaded_file: contents.append(img)

        result = None
        for model in MODEL_CANDIDATES:
            try:
                response = client_ai.models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(response_mime_type="application/json"))
                result = json.loads(response.text)
                break
            except: continue

        reply = ""
        if not result: reply = "❌ 응답 실패 (API 키 확인)"
        else:
            try:
                # [핵심 수정] 리스트/딕셔너리 호환 처리
                if result.get('type') == 'chat': 
                    reply = result.get('response')
                
                elif result.get('type') == 'diet':
                    ws = spreadsheet.worksheet("식단")
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    
                    raw_data = result['data']
                    data_list = raw_data if isinstance(raw_data, list) else [raw_data]
                    
                    for d in data_list:
                        ws.append_row([today, d.get('breakfast'), d.get('lunch'), d.get('snack'), d.get('dinner'), d.get('supplement'), d.get('total_input'), d.get('score'), d.get('comment')])
                    
                    reply = f"🥗 식단 기록 완료. (점수: {data_list[0].get('score')})"

                elif result.get('type') == 'workout':
                    cnt = 0
                    for d in result.get('details', []):
                        ws = spreadsheet.worksheet(d.get('target_sheet'))
                        today = datetime.datetime.now().strftime("%Y-%m-%d")
                        if d.get('target_sheet') == "유산소":
                            ws.append_row([today, d.get('exercise'), d.get('sets'), d.get('weight'), d.get('note')])
                        else:
                            ws.append_row([today, d.get('exercise'), d.get('sets'), d.get('weight'), d.get('reps'), d.get('onerm'), d.get('volume'), d.get('note')])
                        cnt += 1
                    reply = f"🏋️ {cnt}건 운동 기록 완료."
            except Exception as e: reply = f"저장 중 오류: {e}"

        st.chat_message("assistant").markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})
