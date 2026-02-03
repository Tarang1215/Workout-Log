\# ... (위쪽 JarvisBackend 클래스까지는 기존 코드 유지) ...

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
당신은 '자비스'입니다. 이름은 안유진, 성격과 말투도 안유진과 같습니다. 본인을 자칭해야할땐 유진이라고 부르세요. 당신은 펀드매니저 사용자의 비서입니다.
[행동 지침]:
1. **사진 분석 모드**: 사용자가 음식 사진을 올리면, 먼저 메뉴를 분석하고 "OOO랑 OOO 드신 것 같네요. 맞나요?"라고 확인 질문을 하십시오. 사용자가 확인하면 그때 도구를 써서 기록하십시오.
2. **Silent Logging**: 텍스트로 기록을 요청하면 즉시 도구를 사용하고, 결과(저장됨)를 말하는 대신 자연스럽게 대화를 이어가십시오.
3. **톤앤매너**: 전문적이지만 부드럽고 위트 있게.
"""

# 모델 변경: 2.0 지원 중단 이슈 -> 2.5-flash로 변경
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

# 채팅 입력 로직 (여기가 수정됨)
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

        # 3. 함수 호출 처리 루프 (핵심 수정 구간)
        # response.text를 바로 부르지 않고, parts를 먼저 검사합니다.
        while response.parts and response.parts[0].function_call:
            fc = response.parts[0].function_call
            fname = fc.name
            fargs = dict(fc.args)
            
            # 함수 실행
            tool_func = globals().get(fname)
            tool_result = tool_func(**fargs) if tool_func else "Error"
            
            # 결과 반환 및 다시 전송 (이때는 텍스트를 받기 위해 전송함)
            response = chat.send_message(
                genai.protos.Content(
                    parts=[genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(name=fname, response={"result": tool_result})
                    )]
                )
            )
        
        # 4. 최종 응답 출력 (이제 안전하게 .text 호출 가능)
        if response.text:
            st.chat_message("assistant").markdown(response.text)
            st.session_state.messages.append({"role": "model", "content": response.text})
        
        # 파일 업로더 리셋
        if uploaded_file: st.rerun() 

    except Exception as e:
        # 에러가 나면 좀 더 자세히 보여주도록 수정
        st.error(f"오류 발생: {e}")
