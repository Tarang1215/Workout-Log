import streamlit as st
import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime
import json
import time

# ==========================================
# 1. 환경 설정 및 비밀키 로드
# ==========================================
st.set_page_config(page_title="Project Jarvis", page_icon="🤖", layout="wide")

# Streamlit Secrets에서 설정 로드
try:
    if "GEMINI_API_KEY" in st.secrets:
        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
        
        # 구글 시트 인증
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client_sheet = gspread.authorize(creds)
        
        # 시트 이름 설정
        SHEET_NAME = "운동일지_DB" # 기존 시트 이름 유지
        spreadsheet = client_sheet.open(SHEET_NAME)
    else:
        st.error("❌ Secrets 설정이 필요합니다. (.streamlit/secrets.toml 확인)")
        st.stop()
except Exception as e:
    st.error(f"❌ 초기화 오류: {e}")
    st.stop()

# ==========================================
# 2. DB 핸들러 (구글 시트 연동)
# ==========================================
class JarvisDatabase:
    def __init__(self, spreadsheet):
        self.doc = spreadsheet

    def log_diet(self, menu: str, amount: str, meal_type: str):
        """식단을 구글 시트에 기록합니다."""
        try:
            ws = self.doc.worksheet("식단")
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            
            # 식단 시트 컬럼 매핑 (User의 시트 구조에 맞춤)
            # 가정: 날짜(A), 아침(B), 점심(C), 간식(D), 저녁(E), 보충제(F)
            col_map = {"아침": 2, "점심": 3, "간식": 4, "저녁": 5, "보충제": 6}
            target_col = col_map.get(meal_type, 4) # 기본값은 간식
            
            # 오늘 날짜 행 찾기 또는 생성
            cell = ws.find(today)
            input_text = f"{menu} ({amount})"
            
            if cell:
                # 기존 데이터가 있으면 이어쓰기
                existing = ws.cell(cell.row, target_col).value
                new_val = f"{existing}, {input_text}" if existing else input_text
                ws.update_cell(cell.row, target_col, new_val)
            else:
                # 새 행 추가
                row_data = [today, "", "", "", "", "", ""]
                row_data[target_col-1] = input_text
                ws.append_row(row_data)
                
            return "success"
        except Exception as e:
            return f"error: {str(e)}"

    def log_workout(self, exercise: str, log_details: str):
        """운동을 구글 시트에 기록합니다."""
        try:
            # 운동 종목에 따라 시트 분류 (간소화된 로직)
            target_sheet = "기타"
            if any(x in exercise for x in ["벤치", "가슴", "푸시업"]): target_sheet = "가슴"
            elif any(x in exercise for x in ["로우", "풀업", "등"]): target_sheet = "등"
            elif any(x in exercise for x in ["스쿼트", "런지", "하체"]): target_sheet = "하체"
            elif any(x in exercise for x in ["러닝", "유산소", "사이클"]): target_sheet = "유산소"
            
            ws = self.doc.worksheet(target_sheet)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            
            # [날짜, 종목, 내용] 형태로 단순 저장 (추후 상세화 가능)
            ws.append_row([today, exercise, log_details])
            return "success"
        except Exception as e:
            return f"error: {str(e)}"

db = JarvisDatabase(spreadsheet)

# ==========================================
# 3. Gemini 도구(Tool) 정의
# ==========================================
# Gemini가 인식할 수 있는 함수 래퍼
def tool_log_diet(menu: str, amount: str = "적당량", meal_type: str = "간식"):
    """
    사용자가 먹은 음식을 기록할 때 사용합니다.
    Args:
        menu: 음식 이름 (예: 치즈케이크, 닭가슴살)
        amount: 먹은 양 (예: 1조각, 200g)
        meal_type: 식사 종류 (아침, 점심, 저녁, 간식, 보충제 중 하나)
    """
    result = db.log_diet(menu, amount, meal_type)
    if result == "success":
        # ⭐ 핵심: 여기서 Toast 알림을 띄웁니다!
        st.toast(f"🥗 식단 기록 완료: {menu} ({amount})", icon="✅")
        return {"status": "success", "msg": "식단이 데이터베이스에 저장되었습니다."}
    else:
        return {"status": "error", "msg": result}

def tool_log_workout(exercise: str, details: str):
    """
    사용자가 수행한 운동을 기록할 때 사용합니다.
    Args:
        exercise: 운동 이름 (예: 벤치프레스, 러닝)
        details: 세트, 무게, 횟수 등 상세 내용 (예: 100kg 5회 5세트)
    """
    result = db.log_workout(exercise, details)
    if result == "success":
        # ⭐ 핵심: 여기서 Toast 알림을 띄웁니다!
        st.toast(f"💪 운동 기록 완료: {exercise}", icon="🔥")
        return {"status": "success", "msg": "운동이 데이터베이스에 저장되었습니다."}
    else:
        return {"status": "error", "msg": result}

# 도구 딕셔너리 (실제 실행용)
tools_map = {
    "tool_log_diet": tool_log_diet,
    "tool_log_workout": tool_log_workout
}

# ==========================================
# 4. 시스템 프롬프트 및 모델 초기화
# ==========================================
SYSTEM_INSTRUCTION = """
당신은 'Project Jarvis'의 AI 비서입니다. 사용자는 펀드매니저이며, 2년 내 1억 모으기가 목표입니다.
당신의 역할은 사용자의 완벽한 파트너가 되는 것입니다.

[핵심 행동 강령]:
1. **페르소나**: 유능하고, 위트 있고, 공감 능력이 뛰어납니다. 딱딱한 기계처럼 굴지 마십시오.
2. **도구 사용 (Silent Logging)**: 
   - 사용자가 식단이나 운동 정보를 말하면, 즉시 제공된 도구(`tool_log_diet`, `tool_log_workout`)를 사용하여 기록하십시오.
   - **중요**: 도구를 사용한 후, "기록했습니다"라고 말하지 마십시오. 사용자는 이미 화면 알림을 보았습니다.
   - 대신, 대화의 맥락을 이어가십시오. (예: "치즈케이크 기록해줘" -> (기록 실행) -> "맛있었겠네요! 어느 카페 거에요?")
3. **기록 확인**: 사용자가 명시적으로 "오늘 뭐 먹었지?"라고 물을 때만 기록된 내용을 읊어주십시오.

[사용자 프로필]:
- 직업: 펀드매니저 (금융/투자 이야기 환영)
- 관심사: 바이오, 반도체, 의료AI, 헬스
"""

model = genai.GenerativeModel(
    model_name="gemini-2.0-flash-exp", # 함수 호출 성능이 좋은 최신 모델 권장
    tools=[tool_log_diet, tool_log_workout],
    system_instruction=SYSTEM_INSTRUCTION
)

# ==========================================
# 5. 채팅 인터페이스 (Main Loop)
# ==========================================
if "messages" not in st.session_state:
    st.session_state.messages = []
    
# 채팅 히스토리 렌더링
for msg in st.session_state.messages:
    # Function Call 결과 메시지는 사용자에게 보여주지 않음 (깔끔한 UI)
    if msg["role"] != "function":
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# 채팅 처리 (Manual Tool Handling Pattern)
if prompt := st.chat_input("자비스에게 말 걸기..."):
    # 1. 사용자 입력 표시
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    # 2. Gemini 호출 (히스토리 포함)
    try:
        # 히스토리 포맷 변환 (Gemini API 규격에 맞게)
        chat_history = []
        for m in st.session_state.messages:
             # role이 'function'인 것은 내부 처리용이라 제외하거나, 
             # API에 맞게 변환해야 하는데, 여기서는 간단히 user/model만 필터링해서 컨텍스트로 줌
             if m["role"] in ["user", "model"]:
                 chat_history.append({"role": m["role"], "parts": [m["content"]]})
        
        chat = model.start_chat(history=chat_history)
        response = chat.send_message(prompt)
        
        # 3. 함수 호출(Function Call) 처리 루프
        # Gemini가 함수를 호출하고 싶으면 response.parts에 function_call이 들어있음
        final_response_text = ""
        
        # 멀티턴 처리를 위해 while 루프 사용 (함수 호출 -> 결과 반환 -> 다시 모델 생성 -> 텍스트 나올 때까지)
        while response.parts and response.parts[0].function_call:
            fc = response.parts[0].function_call
            fname = fc.name
            fargs = dict(fc.args)
            
            # 함수 실행 및 Toast 출력
            if fname in tools_map:
                tool_result = tools_map[fname](**fargs)
                
                # 결과값을 다시 모델에게 던져줌 (그래야 모델이 "아, 기록됐구나" 하고 다음 말을 함)
                response = chat.send_message(
                    genai.protos.Content(
                        parts=[genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name=fname,
                                response=tool_result
                            )
                        )]
                    )
                )
            else:
                break # 모르는 함수면 중단

        # 4. 최종 텍스트 응답 표시
        final_response_text = response.text
        st.chat_message("assistant").markdown(final_response_text)
        st.session_state.messages.append({"role": "model", "content": final_response_text})

    except Exception as e:
        st.error(f"오류 발생: {e}")
