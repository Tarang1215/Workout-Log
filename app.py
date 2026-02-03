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
import streamlit as st
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ==========================================
# 1. 환경 설정 및 루틴
# ==========================================
st.set_page_config(page_title="Google Workout", page_icon="💪", layout="wide")
SHEET_NAME = "운동일지_DB"

USER_ROUTINE = """
**[Routine]**
- 월: 휴식 / 화: 가슴 / 수: 등 / 목: 어깨 / 금: 휴식 / 토: 하체 / 일: 팔, 복근, 인터벌
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
"""

JSON_GUIDE = """
**[작동 규칙]**
1. **식단 기록 (자연어 처리):**
   - User가 "점심에 A랑 B 먹었어"라고 하면 -> {"lunch": "A + B"} 형태로 변환.
   - 아침, 점심, 저녁, 간식 중 언급된 것만 채우고 나머지는 null.
   - Total Input과 Score는 비워둘 것 (나중에 '식단 빈칸 계산' 버튼으로 채움).
   { "type": "diet", "data": { "breakfast": "...", "lunch": "...", "snack": "...", "dinner": "...", "supplement": "..." } }

2. **운동 기록:** - 세트별 무게 다르면 "20, 40, 60" (콤마 구분).
   - 유산소는 sets=분, weight=강도.
   { "type": "workout", "details": [ { "target_sheet": "...", "exercise": "...", "sets": "...", "weight": "...", "reps": "...", "note": "..." } ] }
   
3. **단순 대화:** { "type": "chat", "response": "..." }
"""
# 현재 시간 및 요일 정보 가져오기
now = datetime.datetime.now()
weekday_map = ["월", "화", "수", "목", "금", "토", "일"]
today_str = now.strftime("%Y-%m-%d")
today_weekday = weekday_map[now.weekday()]

# 최근 운동 기록 요약 가져오기 (지능형 제안을 위해)
def get_recent_workout_summary():
    try:
        ws = spreadsheet.worksheet("통합로그")
        # 마지막 3일치 기록 가져오기
        recent_rows = ws.get_all_values()[-3:]
        return str(recent_rows)
    except:
        return "최근 기록 없음"

# 자비스 전용 시스템 프롬프트 구성
def get_jarvis_system_prompt():
    recent_logs = get_recent_workout_summary()
    profile = get_user_profile()
    
    return f"""
너는 유능하고 위트 있는 개인 비서 '자비스'다. 
[사용자 정보]: {profile}
[기본 루틴]: {USER_ROUTINE}
[현재 시간]: {today_str} ({today_weekday}요일)
[최근 운동 기록]: {recent_logs}

[행동 지침]:
1. 대화를 우선시하라. 사용자가 "저녁에 뭐 먹을까?"라고 물으면 식단 시트에 바로 적지 말고 메뉴를 추천하며 대화하라.
2. 사용자가 "먹었어", "했어", "기록해줘"라고 명확히 말할 때만 JSON의 type을 'diet'나 'workout'으로 출력하라.
3. **지능적 제안**: 최근 기록을 보고 원래 루틴과 다르면 언급하라. 
   - 예: 어제 루틴이 '가슴'인데 기록이 없다면, "어제 가슴 운동을 못 하신 것 같은데, 오늘 가슴 운동을 진행할까요?"라고 먼저 물어봐라.
4. 말투는 정중하면서도 친근한 존댓말을 사용하라.
"""
# ==========================================
# 3. 핵심 함수들 (전체 복구됨)
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

# [기능 1] 운동 계산 및 코멘트
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
                    
                    # A. 유산소
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

                    # B. 근력
                    else:
                        sets_str = str(row[idx_set]).strip()
                        w_str = str(row[idx_w]).strip()
                        r_str = str(row[idx_r]).strip()
                        current_vol = row[idx_vol] if len(row) > idx_vol else ""

                        # 계산
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
                                    if len(reps) > 1: vol_val = w_val * sum(reps)
                                    else:
                                        r_val = reps[0] if reps else 0
                                        vol_val = w_val * r_val * sets_val
                                    onerm_val = w_val * (1 + (reps[0] if reps else 0)/30)

                                ws.update_cell(i, idx_1rm + 1, int(onerm_val))
                                ws.update_cell(i, idx_vol + 1, int(vol_val))
                            except: pass

                        # 코멘트
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

# [기능 2] 식단 빈칸 채우기 (Total Input, Score)
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

# [기능 3] 통합로그 취합
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
            for i, val in enumerate(row_data): summ_ws.update_cell(cell.row, i+1, val)
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
        
        msg = MIMEMultipart()
        msg['From'] = GMAIL_ID
        msg['To'] = GMAIL_ID
        msg['Subject'] = f"[{datetime.datetime.now().strftime('%Y-%m-%d')}] 주간 운동 리포트"
        msg.attach(MIMEText(response.text, 'plain'))

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
                # [핵심 수정] 리스트/딕셔너리 호환 처리 (이제 에러 안 남)
                raw_data = result
                # 리스트면 첫 번째 요소 사용, 딕셔너리면 그대로 사용
                if isinstance(raw_data, list):
                    response_obj = raw_data[0]
                    data_list = raw_data
                else:
                    response_obj = raw_data
                    data_list = [raw_data.get('data')] if raw_data.get('type') == 'diet' else [raw_data.get('details')]

                # 타입별 처리
                if response_obj.get('type') == 'chat': 
                    reply = response_obj.get('response')
                
                elif response_obj.get('type') == 'diet':
                    ws = spreadsheet.worksheet("식단")
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    
                    # 오늘 날짜 행 찾기 (없으면 추가, 있으면 업데이트)
                    cell = ws.find(today)
                    
                    # 리스트가 아니라 data 객체 자체를 가져옴
                    diet_data = response_obj.get('data', {})
                    
                    # 업데이트할 내용 매핑
                    col_map = {
                        2: diet_data.get('breakfast'),
                        3: diet_data.get('lunch'),
                        4: diet_data.get('snack'),
                        5: diet_data.get('dinner'),
                        6: diet_data.get('supplement')
                    }
                    
                    if cell:
                        # 이미 오늘 행이 있으면 빈칸만 채우거나 덮어쓰기
                        for col, val in col_map.items():
                            if val: ws.update_cell(cell.row, col, val)
                        reply = f"🥗 오늘 식단 업데이트 완료: {diet_data}"
                    else:
                        # 오늘 행이 없으면 새로 추가
                        ws.append_row([today, diet_data.get('breakfast'), diet_data.get('lunch'), diet_data.get('snack'), diet_data.get('dinner'), diet_data.get('supplement'), "", "", ""])
                        reply = f"🥗 식단 기록 완료."

                elif response_obj.get('type') == 'workout':
                    cnt = 0
                    # details가 리스트임
                    details = response_obj.get('details', [])
                    for d in details:
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

