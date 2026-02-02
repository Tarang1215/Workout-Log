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
# 1. 환경 설정
# ==========================================
st.set_page_config(page_title="Google Workout", page_icon="💪", layout="wide")
SHEET_NAME = "운동일지_DB"

MODEL_CANDIDATES = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview", 
    "gemini-2.5-flash",
]

# Secrets 인증
try:
    if "GEMINI_API_KEY" in st.secrets:
        GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
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
# 2. 정밀 채점 알고리즘 (업데이트됨)
# ==========================================
SCORING_RULES = """
**[User 스펙 및 목표 (수정됨)]**
- 키/체중: 183cm / **82kg**
- 골격근량: 41kg (상급자)
- 목표: 체지방 10% 커팅 + 근손실 방지
- **단백질 섭취 가이드:** 신장 부담을 줄이기 위해 **체중 x 1.5 ~ 2.0g (약 123g ~ 164g)**을 목표로 함. 
  (무조건 많이 먹는다고 점수 주지 말고, 이 범위를 충족하면 만점 처리할 것)

**[정밀 채점 로직]**
1. **단백질:** 120g 미만이면 감점. 165g을 과도하게 초과해도 가산점 없음.
2. **운동&탄수화물:** 운동한 날은 탄수화물 섭취를 '회복'으로 인정. 운동 안 한 날의 고탄수화물은 '지방 축적'으로 간주하여 감점.
3. **식품 포맷:** "음식1 + 음식2 + 음식3" 형태로 기록됨. 이를 합산하여 평가할 것.
"""

JSON_GUIDE = f"""
**[작동 규칙]**
1. 식단 기록: 
   - 사용자가 "닭가슴살 + 햇반" 처럼 입력하면 그대로 기록.
   - Total Input: "C:xxx P:xxx F:xxx (비율)"
   - Comment: I열에 들어갈 피드백
   {{ "type": "diet", "data": {{ "breakfast": "...", "lunch": "...", "total_input": "...", "score": "...", "comment": "..." }} }}
2. 운동 기록:
   - 세트마다 무게가 다르면 "20, 40, 60" 처럼 콤마로 구분하여 저장.
   {{ "type": "workout", "details": [ {{ "target_sheet": "...", "exercise": "...", "sets": "...", "weight": "...", "reps": "...", "note": "..." }} ] }}
"""

# ==========================================
# 3. 비즈니스 로직
# ==========================================
def get_user_profile():
    try:
        return "\n".join([f"- {row[0]}: {row[1]}" for row in spreadsheet.worksheet("프로필").get_all_values() if len(row) >= 2])
    except: return "프로필 없음"

def get_workout_volume_dict():
    """날짜별 운동 요약 (점수 계산용)"""
    try:
        ws = spreadsheet.worksheet("통합로그")
        rows = ws.get_all_values()
        vol_dict = {}
        for row in rows[1:]:
            if len(row) > 4:
                vol_dict[row[0]] = f"{row[1]} / {row[4]}kg"
        return vol_dict
    except: return {}

def calculate_past_workout_stats():
    """
    [기능] 
    1. 수학적 계산: 볼륨, 1RM (콤마 구분 처리 완벽 지원)
    2. AI 분석: 비고(Note)란이 비어있으면 AI가 짧은 코멘트 작성
    """
    try:
        sheet_list = ["등", "가슴", "하체", "어깨", "이두", "삼두", "복근", "기타"]
        total_updated = 0
        
        for sheet_name in sheet_list:
            try:
                ws = spreadsheet.worksheet(sheet_name)
                rows = ws.get_all_values()
                if len(rows) < 2: continue
                
                # 헤더 찾기
                header = rows[0]
                try:
                    idx_set = next(i for i, h in enumerate(header) if "세트" in h)
                    idx_w = next(i for i, h in enumerate(header) if "무게" in h)
                    idx_r = next(i for i, h in enumerate(header) if "횟수" in h)
                    idx_1rm = next(i for i, h in enumerate(header) if "1RM" in h)
                    idx_vol = next(i for i, h in enumerate(header) if "볼륨" in h)
                    idx_note = next(i for i, h in enumerate(header) if "비고" in h)
                except: continue

                updates_needed = False
                
                for i, row in enumerate(rows[1:], start=2):
                    # 데이터 읽기
                    sets_str = str(row[idx_set]).strip()
                    w_str = str(row[idx_w]).strip()
                    r_str = str(row[idx_r]).strip()
                    current_vol = row[idx_vol] if len(row) > idx_vol else ""
                    current_note = row[idx_note] if len(row) > idx_note else ""

                    # 1. 수학적 계산 (볼륨이 비어있으면)
                    if not current_vol and w_str and r_str:
                        try:
                            # 숫자 추출 (콤마 분리)
                            weights = [float(x) for x in re.findall(r"[\d\.]+", w_str)]
                            reps = [float(x) for x in re.findall(r"[\d\.]+", r_str)]
                            sets_val = float(re.findall(r"[\d\.]+", sets_str)[0]) if re.findall(r"[\d\.]+", sets_str) else 1.0

                            vol_val = 0
                            onerm_val = 0

                            # Case A: 무게가 여러 개 (피라미드 세트) "20, 40, 60"
                            if len(weights) > 1:
                                # 횟수도 여러 개면 1:1 매칭, 아니면 마지막 횟수 반복
                                if len(reps) == len(weights):
                                    vol_val = sum(w * r for w, r in zip(weights, reps))
                                else:
                                    # 횟수가 하나만 적혀있으면(예: 10) 모든 세트 10회로 가정
                                    r_val = reps[0] if reps else 0
                                    vol_val = sum(w * r_val for w in weights)
                                
                                max_w = max(weights)
                                # 1RM은 최고 무게 기준
                                r_at_max = reps[weights.index(max_w)] if len(reps) > weights.index(max_w) else (reps[0] if reps else 0)
                                onerm_val = max_w * (1 + r_at_max/30)

                            # Case B: 무게가 하나 (고정 세트) "100"
                            else:
                                w_val = weights[0]
                                # 횟수가 여러 개? "12, 10, 8" -> 다 더해서 무게 곱함
                                if len(reps) > 1:
                                    vol_val = w_val * sum(reps)
                                    max_r = max(reps) # 1RM은 가장 많이 한 횟수 기준? 보통 첫세트 기준
                                    onerm_val = w_val * (1 + reps[0]/30)
                                # 횟수도 하나? "10" -> 무게 x 횟수 x 세트수
                                else:
                                    r_val = reps[0] if reps else 0
                                    vol_val = w_val * r_val * sets_val
                                    onerm_val = w_val * (1 + r_val/30)

                            ws.update_cell(i, idx_1rm + 1, int(onerm_val))
                            ws.update_cell(i, idx_vol + 1, int(vol_val))
                            total_updated += 1
                        except: pass
                    
                    # 2. AI 코멘트 작성 (비고가 비어있고 운동 데이터가 있으면)
                    if not current_note and w_str:
                        try:
                            prompt = f"""
                            헬스 코치로서 이 운동 세트에 대한 한 줄 피드백을 작성해. (존댓말)
                            종목: {row[1]}, 세트: {sets_str}, 무게: {w_str}, 횟수: {r_str}
                            User: 82kg 상급자.
                            """
                            response = client_ai.models.generate_content(
                                model="gemini-3-flash-preview", 
                                contents=prompt
                            )
                            comment = response.text.strip()
                            ws.update_cell(i, idx_note + 1, comment)
                            time.sleep(1) # 과부하 방지
                        except: pass

            except: continue
        return f"근력 운동 {total_updated}건 계산 및 코멘트 작성 완료"
    except Exception as e: return f"오류: {e}"

def fill_past_diet_blanks(profile_txt):
    """식단 빈칸 채우기 (Total Input, Score, Comment)"""
    try:
        ws = spreadsheet.worksheet("식단")
        rows = ws.get_all_values()
        
        try:
            idx_total = next(i for i, h in enumerate(rows[0]) if "Total" in h)
            idx_score = next(i for i, h in enumerate(rows[0]) if "Score" in h)
            idx_comment = 8 
        except: return "식단 시트 헤더 확인 필요"

        workout_history = get_workout_volume_dict()
        updates_needed = []
        
        for i, row in enumerate(rows[1:], start=2):
            is_empty = (len(row) <= idx_total) or (not row[idx_total])
            has_content = any(row[j] for j in range(1, idx_total) if len(row) > j and row[j])
            
            if is_empty and has_content:
                date = row[0]
                workout_info = workout_history.get(date, "휴식")
                # 식단 데이터 (음식1 + 음식2 포맷)
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
            
        if not result: return "AI 응답 실패 (API Key 확인)"

        cnt = 0
        for item in result:
            ws.update_cell(item['row'], idx_total + 1, item['total_input'])
            ws.update_cell(item['row'], idx_score + 1, item['score'])
            ws.update_cell(item['row'], idx_comment + 1, item['comment'])
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
        st.info("수학적 계산과 AI 코멘트 작성을 동시에 진행합니다. (시간이 걸릴 수 있습니다)")
        with st.spinner("처리 중..."): 
            st.success(calculate_past_workout_stats())
        
    if st.button("🥗 식단 빈칸 계산"):
        with st.spinner("AI 분석 중..."): 
            msg = fill_past_diet_blanks(get_user_profile())
            if "실패" in msg: st.error(msg)
            else: st.success(msg)

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
        if not result: reply = "❌ 응답 실패 (API Key 확인)"
        else:
            try:
                if result.get('type') == 'chat': reply = result.get('response')
                elif result.get('type') == 'diet':
                    ws = spreadsheet.worksheet("식단")
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    d = result['data']
                    ws.append_row([today, d.get('breakfast'), d.get('lunch'), d.get('snack'), d.get('dinner'), d.get('supplement'), d.get('total_input'), d.get('score'), d.get('comment')])
                    reply = f"🥗 기록 완료: {d.get('total_input')} / 점수: {d.get('score')}점"
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
                    reply = f"🏋️ {cnt}건 기록 완료."
            except Exception as e: reply = f"저장 중 오류: {e}"

        st.chat_message("assistant").markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})
