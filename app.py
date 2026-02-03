import streamlit as st
import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime
import json
import time
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from PIL import Image

# ==========================================
# 1. 환경 설정 및 초기화
# ==========================================
st.set_page_config(page_title="Project Jarvis", page_icon="🕶️", layout="wide")

# CSS로 토스트 메시지 스타일링 및 팝오버 조정
st.markdown("""
<style>
    .stToast { background-color: #333; color: white; border-radius: 10px; }
</style>
""", unsafe_allow_html=True)

# Secrets 로드
try:
    if "GEMINI_API_KEY" in st.secrets:
        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
        
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client_sheet = gspread.authorize(creds)
        spreadsheet = client_sheet.open("운동일지_DB") # 시트 이름 정확히 확인!
        
        GMAIL_ID = st.secrets.get("GMAIL_ID")
        GMAIL_PW = st.secrets.get("GMAIL_APP_PW")
    else:
        st.error("❌ Secrets 설정이 필요합니다.")
        st.stop()
except Exception as e:
    st.error(f"❌ 초기화 오류: {e}")
    st.stop()

# ==========================================
# 2. Jarvis Backend (기능 처리 엔진)
# ==========================================
class JarvisBackend:
    def __init__(self, doc):
        self.doc = doc

    # [Tool 1] 식단 기록
    def log_diet(self, menu: str, amount: str, meal_type: str):
        try:
            ws = self.doc.worksheet("식단")
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            col_map = {"아침": 2, "점심": 3, "간식": 4, "저녁": 5, "보충제": 6}
            target_col = col_map.get(meal_type, 4)
            
            cell = ws.find(today)
            input_text = f"{menu}({amount})"
            
            if cell:
                existing = ws.cell(cell.row, target_col).value
                new_val = f"{existing}, {input_text}" if existing else input_text
                ws.update_cell(cell.row, target_col, new_val)
            else:
                row_data = [today, "", "", "", "", "", ""]
                row_data[target_col-1] = input_text
                ws.append_row(row_data)
            return "success"
        except Exception as e: return f"error: {e}"

    # [Tool 2] 운동 기록
    def log_workout(self, exercise: str, details: str):
        try:
            target_sheet = "기타"
            if any(x in exercise for x in ["벤치", "가슴", "푸시업"]): target_sheet = "가슴"
            elif any(x in exercise for x in ["로우", "풀업", "등"]): target_sheet = "등"
            elif any(x in exercise for x in ["스쿼트", "런지", "하체"]): target_sheet = "하체"
            elif any(x in exercise for x in ["러닝", "유산소", "사이클"]): target_sheet = "유산소"
            
            ws = self.doc.worksheet(target_sheet)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            ws.append_row([today, exercise, details])
            return "success"
        except Exception as e: return f"error: {e}"

    # [Batch 1] 운동 통계 일괄 계산 (1RM, 볼륨, AI코멘트)
    def batch_calculate_stats(self):
        sheet_list = ["등", "가슴", "하체", "어깨", "이두", "삼두", "복근"]
        count = 0
        
        for sheet_name in sheet_list:
            try:
                ws = self.doc.worksheet(sheet_name)
                rows = ws.get_all_values()
                if len(rows) < 2: continue
                
                header = rows[0]
                try:
                    idx_w = next(i for i, h in enumerate(header) if "무게" in h)
                    idx_r = next(i for i, h in enumerate(header) if "횟수" in h)
                    idx_vol = next(i for i, h in enumerate(header) if "볼륨" in h)
                    idx_1rm = next(i for i, h in enumerate(header) if "1RM" in h)
                    idx_note = next(i for i, h in enumerate(header) if "비고" in h)
                except: continue

                for i, row in enumerate(rows[1:], start=2):
                    # 볼륨이 비어있고 무게/횟수가 있으면 계산 대상
                    if (len(row) <= idx_vol or not row[idx_vol]) and row[idx_w] and row[idx_r]:
                        w_str = row[idx_w]
                        r_str = row[idx_r]
                        weights = [float(x) for x in re.findall(r"[\d\.]+", w_str)]
                        reps = [float(x) for x in re.findall(r"[\d\.]+", r_str)]
                        
                        if weights and reps:
                            max_w = max(weights)
                            max_r = reps[0] if reps else 0
                            
                            # 1RM & 볼륨 계산
                            one_rm = int(max_w * (1 + max_r/30))
                            vol = int(max_w * sum(reps)) if len(weights) == 1 else int(sum(w*r for w,r in zip(weights, reps)) if len(weights)==len(reps) else max_w * sum(reps))
                            
                            ws.update_cell(i, idx_vol+1, vol)
                            ws.update_cell(i, idx_1rm+1, one_rm)
                            
                            # AI 코멘트 (비어있으면)
                            current_note = row[idx_note] if len(row) > idx_note else ""
                            if not current_note:
                                prompt = f"헬스 트레이너로서 짧고 굵은 피드백(반말). 종목:{row[1]}, 무게:{w_str}, 횟수:{r_str}, 1RM:{one_rm}."
                                model_flash = genai.GenerativeModel("gemini-2.5-flash") # 빠른 모델 사용
                                res = model_flash.generate_content(prompt)
                                ws.update_cell(i, idx_note+1, res.text.strip())
                            
                            count += 1
                            time.sleep(0.8) # API 제한 방지
            except: continue
        return f"✅ 총 {count}건 업데이트 완료!"

    # [Batch 2] 식단 일괄 채점
    def batch_score_diet(self):
        try:
            ws = self.doc.worksheet("식단")
            rows = ws.get_all_values()
            idx_total = next(i for i, h in enumerate(rows[0]) if "Total" in h)
            idx_score = next(i for i, h in enumerate(rows[0]) if "Score" in h)
            idx_cmt = 8
            
            updates = []
            for i, row in enumerate(rows[1:], start=2):
                has_food = any(row[j] for j in range(1, idx_total) if len(row) > j and row[j])
                is_empty_score = (len(row) <= idx_score) or (not row[idx_score])
                
                if has_food and is_empty_score:
                    diet_str = f"아침:{row[1]}, 점심:{row[2]}, 저녁:{row[4]}, 간식:{row[3]}"
                    updates.append((i, diet_str))
            
            if not updates: return "채점할 데이터가 없습니다."

            count = 0
            model_flash = genai.GenerativeModel("gemini-2.5-flash")
            for row_idx, diet_str in updates:
                prompt = f"""
                영양사로서 평가해줘. User: 183cm/82kg/골격근41kg (커팅중).
                식단: {diet_str}
                Output JSON: {{ "total": "C:.. P:.. F:..", "score": 85, "comment": "한줄평" }}
                """
                try:
                    res = model_flash.generate_content(prompt)
                    txt = res.text.strip().replace("```json", "").replace("```", "")
                    data = json.loads(txt)
                    
                    ws.update_cell(row_idx, idx_total+1, data.get("total", "-"))
                    ws.update_cell(row_idx, idx_score+1, data.get("score", 0))
                    ws.update_cell(row_idx, idx_cmt+1, data.get("comment", "-"))
                    count += 1
                    time.sleep(1)
                except: continue
            return f"✅ {count}일치 식단 채점 완료"
        except Exception as e: return f"오류: {e}"

    # [Batch 3] 주간 리포트 발송
    def send_report(self):
        if not GMAIL_ID: return "❌ 이메일 설정 필요"
        try:
            ws = self.doc.worksheet("통합로그")
            logs = ws.get_all_values()[-7:]
            model_pro = genai.GenerativeModel("gemini-3-flash-preview")
            
            prompt = f"자비스로서 사용자의 지난주 운동/식단 요약 보고서를 작성해. 데이터: {logs}. 정중하고 분석적으로."
            res = model_pro.generate_content(prompt)
            
            msg = MIMEMultipart()
            msg['From'] = GMAIL_ID
            msg['To'] = GMAIL_ID
            msg['Subject'] = f"[Jarvis] 주간 리포트 ({datetime.datetime.now().strftime('%Y-%m-%d')})"
            msg.attach(MIMEText(res.text, 'plain'))
            
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(GMAIL_ID, GMAIL_PW)
            server.sendmail(GMAIL_ID, GMAIL_ID, msg.as_string())
            server.quit()
            return "📧 리포트 발송 완료!"
        except Exception as e: return f"전송 실패: {e}"

backend = JarvisBackend(spreadsheet)

# ==========================================
# 3. Gemini Tools & System Prompt
# ==========================================
def tool_log_diet(menu: str, amount: str = "1인분", meal_type: str = "간식"):
    """식단을 기록합니다. 식사 메뉴와 양, 종류(아침/점심/저녁/간식)를 받습니다."""
    res = backend.log_diet(menu, amount, meal_type)
    if res == "success":
        st.toast(f"🥗 기록 완료: {menu}", icon="✅")
        return "데이터베이스 저장 완료."
    return "저장 실패"

def tool_log_workout(exercise: str, details: str):
    """운동을 기록합니다. 종목명과 상세내용(무게, 횟수 등)을 받습니다."""
    res = backend.log_workout(exercise, details)
    if res == "success":
        st.toast(f"💪 기록 완료: {exercise}", icon="🔥")
        return "데이터베이스 저장 완료."
    return "저장 실패"

tools = [tool_log_diet, tool_log_workout]

SYSTEM_PROMPT = """
당신은 '자비스'입니다. 이름은 안유진이고 성격과 말투도 안유진과 같습니다. 본인을 자칭해야할땐 유진이라고 부르세요. 당신은 사용자의 비서역할을 수행합니다.
[행동 지침]:
1. **사진 분석 모드**: 사용자가 음식 사진을 올리면, 먼저 메뉴를 분석하고 "OOO랑 OOO 드신 것 같네요. 맞나요?"라고 확인 질문을 하십시오. 사용자가 확인하면 그때 도구를 써서 기록하십시오.
2. **Silent Logging**: 텍스트로 기록을 요청하면 즉시 도구를 사용하고, 결과(저장됨)를 말하는 대신 자연스럽게 대화를 이어가십시오.
3. **톤앤매너**: 전문적이지만 부드럽고 위트 있게.
"""

# 모델 설정: gemini-2.5-flash 사용
model = genai.GenerativeModel("gemini-2.5-flash", tools=tools, system_instruction=SYSTEM_PROMPT)

# ==========================================
# 4. Streamlit UI (사이드바 & 메인)
# ==========================================
st.title("Project Jarvis 🕶️")

# [사이드바] 일괄 처리 버튼 모음
with st.sidebar:
    st.header("🎛️ Control Center")
    if st.button("🏋️ 지난 운동 계산 & 피드백"):
        with st.spinner("계산 중..."): st.success(backend.batch_calculate_stats())
    
    if st.button("🥗 식단 빈칸 채점"):
        with st.spinner("채점 중..."): st.success(backend.batch_score_diet())
        
    if st.button("📧 주간 리포트 발송"):
        with st.spinner("작성 중..."): st.success(backend.send_report())
    
    st.divider()
    st.caption("Developed by Jarvis Project Team")

# [메인] 채팅 & 사진 입력
if "messages" not in st.session_state:
    st.session_state.messages = []

# 이전 대화 출력
for msg in st.session_state.messages:
    if msg["role"] != "function":
        with st.chat_message(msg["role"]):
            if "image" in msg: st.image(msg["image"], width=250)
            st.markdown(msg["content"])

# [UI 핵심] 사진 업로더를 팝오버로 숨김
with st.popover("📸 사진 추가 / 분석", use_container_width=True):
    uploaded_file = st.file_uploader("음식 또는 운동 사진을 올려주세요", type=['jpg', 'png', 'jpeg'])

# 채팅 입력 및 처리 로직 (수정 완료)
if prompt := st.chat_input("Waiting for your chat..."):
    # 1. 유저 메시지 표시
    with st.chat_message("user"):
        if uploaded_file:
            img = Image.open(uploaded_file)
            st.image(img, width=250)
            st.session_state.messages.append({"role": "user", "content": "[사진 제출]", "image": img})
        st.markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

    # 2. AI 처리
    try:
        # 히스토리 구성
        history_for_api = []
        for m in st.session_state.messages:
            if m["role"] == "user":
                parts = [m["content"]]
                if "image" in m: parts.append(m["image"])
                history_for_api.append({"role": "user", "parts": parts})
            elif m["role"] == "model":
                history_for_api.append({"role": "model", "parts": [m["content"]]})

        # 이번 턴 메시지 구성
        current_parts = [prompt]
        if uploaded_file and not any("image" in m for m in st.session_state.messages[-1:]):
             current_parts.append(Image.open(uploaded_file))

        chat = model.start_chat(history=history_for_api[:-1])
        response = chat.send_message(current_parts)

        # 3. 함수 호출 처리 루프 (안전 장치 추가)
        while response.parts and response.parts[0].function_call:
            fc = response.parts[0].function_call
            fname = fc.name
            fargs = dict(fc.args)
            
            tool_func = globals().get(fname)
            tool_result = tool_func(**fargs) if tool_func else "Error"
            
            # 결과 반환
            response = chat.send_message(
                genai.protos.Content(
                    parts=[genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(name=fname, response={"result": tool_result})
                    )]
                )
            )
        
        # 4. 최종 텍스트 응답 출력
        if response.text:
            st.chat_message("assistant").markdown(response.text)
            st.session_state.messages.append({"role": "model", "content": response.text})
        
        # 파일 업로더 리셋
        if uploaded_file: st.rerun() 

    except Exception as e:
        st.error(f"오류 발생: {e}")
