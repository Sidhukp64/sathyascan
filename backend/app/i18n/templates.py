"""
Malayalam/English/Hindi/Tamil language support (decisions.md: "Malayalam/
English response support initially", extended to Hindi/Tamil per the user's
explicit "audit and fix only the two known Phase 8 limitations" instruction
— see docs/risks-and-open-questions.md's Phase 8 → Phase 8 hardening entry
for the full history). First-pass templates for all four languages — worth
a native-speaker review before wide use, same caveat as the OCR/ASR
benchmarking flagged for Indic languages elsewhere in
docs/risks-and-open-questions.md; these are not yet professionally reviewed
translations.

Two distinct uses:
  - LANGUAGE_NAMES: told to Claude so EvidenceSynthesisTool writes
    reasoning_text directly in the target language (no separate translation
    call — see agent-architecture.md's Phase 2 design note).
  - RESPONSE_LABELS: deterministic string templates used by
    ResponseGenerationTool to assemble the final WhatsApp text — NOT an LLM
    call, so this step is free, fast, and fully testable.

**Adding hi/ta required no code changes outside this file** — every caller
already used the `dict.get(resolve_language(language), dict["en"])` pattern
(see app/agent/image_pipeline.py, app/agent/tools/response_generation.py,
app/agent/pdf_report.py, app/agent/notifications.py) or `LANGUAGE_NAMES.get(
language, language)` (claim_extraction.py, evidence_synthesis.py) — both
already handle an arbitrary key set. Expanding SUPPORTED_LANGUAGES here and
filling in every dict below was sufficient; this was a data change, not an
architecture change.
"""

SUPPORTED_LANGUAGES = {"en", "ml", "hi", "ta"}
DEFAULT_LANGUAGE = "en"

LANGUAGE_NAMES = {
    "en": "English",
    "ml": "Malayalam",
    "hi": "Hindi",
    "ta": "Tamil",
}

RESULT_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "verified": "✅ Verified",
        "false": "❌ False",
        "misleading": "⚠️ Misleading",
        "partially_true": "🟡 Partially True",
        "unverified": "❓ Unverified",
        "insufficient_evidence": "❓ Insufficient Evidence",
        "opinion": "ℹ️ Opinion",
        "satire": "🎭 Satire/Parody",
        "no_claims_detected": "no factual claim detected",
    },
    "ml": {
        "verified": "✅ സ്ഥിരീകരിച്ചു",
        "false": "❌ തെറ്റ്",
        "misleading": "⚠️ തെറ്റിദ്ധരിപ്പിക്കുന്നത്",
        "partially_true": "🟡 ഭാഗികമായി ശരി",
        "unverified": "❓ സ്ഥിരീകരിക്കാനായില്ല",
        "insufficient_evidence": "❓ വിവരങ്ങൾ അപര്യാപ്തം",
        "opinion": "ℹ️ അഭിപ്രായം",
        "satire": "🎭 ആക്ഷേപഹാസ്യം",
        "no_claims_detected": "പരിശോധിക്കാവുന്ന വസ്തുത കണ്ടെത്തിയില്ല",
    },
    "hi": {
        "verified": "✅ सत्यापित",
        "false": "❌ गलत",
        "misleading": "⚠️ भ्रामक",
        "partially_true": "🟡 आंशिक रूप से सही",
        "unverified": "❓ असत्यापित",
        "insufficient_evidence": "❓ अपर्याप्त प्रमाण",
        "opinion": "ℹ️ राय",
        "satire": "🎭 व्यंग्य/पैरोडी",
        "no_claims_detected": "कोई तथ्यात्मक दावा नहीं मिला",
    },
    "ta": {
        "verified": "✅ சரிபார்க்கப்பட்டது",
        "false": "❌ தவறானது",
        "misleading": "⚠️ தவறாக வழிநடத்தும்",
        "partially_true": "🟡 ஓரளவு உண்மை",
        "unverified": "❓ சரிபார்க்கப்படவில்லை",
        "insufficient_evidence": "❓ போதிய ஆதாரம் இல்லை",
        "opinion": "ℹ️ கருத்து",
        "satire": "🎭 நையாண்டி/பகடி",
        "no_claims_detected": "உண்மைச் சார்ந்த கூற்று எதுவும் கண்டறியப்படவில்லை",
    },
}

UI_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "result_header": "🔍 *SathyaScan Result*",
        "claim_label": "*Claim:*",
        "status_label": "*Status:*",
        "why_label": "*Why:*",
        "evidence_label": "*Evidence:*",
        "confidence_label": "*Confidence:*",
        "no_evidence": "No sources were found for this claim.",
        "disclaimer": (
            "_SathyaScan uses AI and may make mistakes. This is not absolute "
            "proof — please check the sources above yourself._"
        ),
        "no_claims_message": (
            "I couldn't find a specific factual claim to check in your message. "
            "Try sending a specific claim, statement, or forwarded message you'd "
            "like verified."
        ),
        "confidence_high": "High",
        "confidence_medium": "Medium",
        "confidence_low": "Low",
        "analyzing_ack": "🔍 Analyzing your message, this may take a moment...",
        "rate_limited": "You're sending messages a little too quickly. Please wait a moment and try again.",
        "account_suspended_message": (
            "Your SathyaScan account has been suspended. Please contact support if you believe this is a mistake."
        ),
        "at_capacity": (
            "SathyaScan is experiencing high demand right now. Please try again "
            "in a few minutes."
        ),
        "processing_error": "SathyaScan is unable to process this request right now. Please try again later.",
        "processing_timeout": "This is taking longer than expected, so we've stopped this attempt. Please try again.",
        "analyzing_image_ack": "🔍 Analyzing your image, this may take a moment...",
        "content_declined": (
            "SathyaScan is unable to process this content. Please try sending "
            "something else."
        ),
        "no_text_found": (
            "I couldn't find any readable text in this image to fact-check. "
            "If it contains a claim, try sending the text directly."
        ),
        "file_too_large": "This image is too large for SathyaScan to process. Please send a smaller image.",
        "unsupported_media": "SathyaScan couldn't recognize this as a supported image format.",
        "visual_analysis_unavailable_note": (
            "(Note: this check is based on text found in the image only — "
            "visual authenticity analysis wasn't available for this request.)"
        ),
        "analyzing_url_ack": "🔍 Checking this link, this may take a moment...",
        "url_safety_header": "🛡️ *Link Safety*",
        "url_label": "*Link:*",
        "risk_label": "*Risk:*",
        "reasons_label": "*Why:*",
        "recommendation_label": "*Recommendation:*",
        "no_safety_concerns": "No specific concerns found.",
        "url_content_unavailable_note": (
            "I couldn't retrieve this page's content, so only the safety check above is available — "
            "no credibility assessment could be made."
        ),
        "url_robots_disallowed_note": (
            "This site's own rules ask automated tools not to access this page, so SathyaScan only checked "
            "the link itself — no credibility assessment could be made."
        ),
        "unsupported_url_content": (
            "This link doesn't point to a webpage SathyaScan can read (it may be a file, image, or video) — "
            "only the safety check above is available."
        ),
        "url_no_claims_message": "I checked this link's safety, but couldn't find a specific factual claim on the page to fact-check.",
        "analyzing_audio_ack": "🔍 Analyzing your voice message, this may take a moment...",
        "audio_result_header": "🔊 *SathyaScan Audio Result*",
        "transcript_label": "📝 *Transcript:*",
        "media_authenticity_label": "🎙️ *Media authenticity:*",
        "transcription_failed": (
            "🎙️ I couldn't reliably transcribe the audio, so I can't safely verify the claim."
        ),
        "no_speech_found": "🎙️ I couldn't detect any speech in this audio to fact-check.",
        "audio_no_claims_message": "I transcribed this audio, but couldn't find a specific factual claim to fact-check.",
        "duration_too_long": "This audio is too long for SathyaScan to process. Please send a shorter clip.",
        "analyzing_video_ack": "🔍 Analyzing your video, this may take a moment...",
        "video_result_header": "🎥 *SathyaScan Video Result*",
        "spoken_claim_label": "📝 *Spoken claim:*",
        "on_screen_text_label": "🖼️ *Text detected in video:*",
        "video_fact_check_status_label": "*Fact-check status:*",
        "video_no_claims_message": "🎥 I couldn't find a specific factual claim to verify in this video.",
        "media_independence_note": (
            "*Important:*\nMedia authenticity and factual verification are independent results."
        ),
        "video_duration_too_long": "This video is too long for SathyaScan to process. Please send a shorter clip.",
    },
    "ml": {
        "result_header": "🔍 *SathyaScan ഫലം*",
        "claim_label": "*അവകാശവാദം:*",
        "status_label": "*നില:*",
        "why_label": "*കാരണം:*",
        "evidence_label": "*തെളിവുകൾ:*",
        "confidence_label": "*ആത്മവിശ്വാസം:*",
        "no_evidence": "ഈ അവകാശവാദത്തിന് സ്രോതസ്സുകളൊന്നും കണ്ടെത്തിയില്ല.",
        "disclaimer": (
            "_SathyaScan AI ഉപയോഗിക്കുന്നു, തെറ്റുകൾ സംഭവിക്കാം. ഇത് സമ്പൂർണ്ണ "
            "തെളിവല്ല — മുകളിലുള്ള സ്രോതസ്സുകൾ സ്വയം പരിശോധിക്കുക._"
        ),
        "no_claims_message": (
            "നിങ്ങളുടെ സന്ദേശത്തിൽ പരിശോധിക്കാവുന്ന ഒരു വസ്തുതാപരമായ അവകാശവാദം "
            "കണ്ടെത്താനായില്ല. പരിശോധിക്കേണ്ട ഒരു നിർദ്ദിഷ്ട അവകാശവാദമോ ഫോർവേഡ് "
            "ചെയ്ത സന്ദേശമോ അയയ്ക്കുക."
        ),
        "confidence_high": "ഉയർന്നത്",
        "confidence_medium": "ഇടത്തരം",
        "confidence_low": "കുറഞ്ഞത്",
        "analyzing_ack": "🔍 നിങ്ങളുടെ സന്ദേശം വിശകലനം ചെയ്യുന്നു, ഇതിന് അല്പസമയം എടുത്തേക്കാം...",
        "rate_limited": "നിങ്ങൾ വളരെ വേഗത്തിൽ സന്ദേശങ്ങൾ അയക്കുന്നു. അല്പസമയം കാത്തിരുന്ന് വീണ്ടും ശ്രമിക്കുക.",
        "account_suspended_message": (
            "നിങ്ങളുടെ SathyaScan അക്കൗണ്ട് സസ്പെൻഡ് ചെയ്തിരിക്കുന്നു. ഇത് ഒരു തെറ്റാണെന്ന് നിങ്ങൾ "
            "കരുതുന്നുവെങ്കിൽ ദയവായി പിന്തുണയുമായി ബന്ധപ്പെടുക."
        ),
        "at_capacity": "SathyaScan ഇപ്പോൾ തിരക്കിലാണ്. ദയവായി കുറച്ച് മിനിറ്റിനുള്ളിൽ വീണ്ടും ശ്രമിക്കുക.",
        "processing_error": "SathyaScan-ന് ഇപ്പോൾ ഈ അഭ്യർത്ഥന പ്രോസസ്സ് ചെയ്യാൻ കഴിയുന്നില്ല. പിന്നീട് വീണ്ടും ശ്രമിക്കുക.",
        "processing_timeout": "പ്രതീക്ഷിച്ചതിലും കൂടുതൽ സമയമെടുക്കുന്നതിനാൽ ഈ ശ്രമം നിർത്തി. ദയവായി വീണ്ടും ശ്രമിക്കുക.",
        "analyzing_image_ack": "🔍 നിങ്ങളുടെ ചിത്രം വിശകലനം ചെയ്യുന്നു, ഇതിന് അല്പസമയം എടുത്തേക്കാം...",
        "content_declined": "ഈ ഉള്ളടക്കം പ്രോസസ്സ് ചെയ്യാൻ SathyaScan-ന് കഴിയില്ല. മറ്റെന്തെങ്കിലും അയയ്ക്കാൻ ശ്രമിക്കുക.",
        "no_text_found": "ഈ ചിത്രത്തിൽ വസ്തുത പരിശോധിക്കാൻ വായിക്കാവുന്ന ടെക്സ്റ്റ് കണ്ടെത്താനായില്ല. ഒരു അവകാശവാദം ഉണ്ടെങ്കിൽ, ടെക്സ്റ്റ് നേരിട്ട് അയയ്ക്കാൻ ശ്രമിക്കുക.",
        "file_too_large": "ഈ ചിത്രം SathyaScan-ന് പ്രോസസ്സ് ചെയ്യാൻ കഴിയാത്തത്ര വലുതാണ്. ദയവായി ചെറിയ ചിത്രം അയയ്ക്കുക.",
        "unsupported_media": "ഇത് പിന്തുണയ്ക്കുന്ന ഒരു ഇമേജ് ഫോർമാറ്റായി SathyaScan തിരിച്ചറിഞ്ഞില്ല.",
        "visual_analysis_unavailable_note": (
            "(കുറിപ്പ്: ഈ പരിശോധന ചിത്രത്തിലെ ടെക്സ്റ്റ് അടിസ്ഥാനമാക്കി മാത്രമുള്ളതാണ് — "
            "ഈ അഭ്യർത്ഥനയ്ക്ക് വിഷ്വൽ ആധികാരികത വിശകലനം ലഭ്യമായിരുന്നില്ല.)"
        ),
        "analyzing_url_ack": "🔍 ഈ ലിങ്ക് പരിശോധിക്കുന്നു, ഇതിന് അല്പസമയം എടുത്തേക്കാം...",
        "url_safety_header": "🛡️ *ലിങ്ക് സുരക്ഷ*",
        "url_label": "*ലിങ്ക്:*",
        "risk_label": "*അപകടസാധ്യത:*",
        "reasons_label": "*കാരണം:*",
        "recommendation_label": "*ശുപാർശ:*",
        "no_safety_concerns": "പ്രത്യേക ആശങ്കകളൊന്നും കണ്ടെത്തിയില്ല.",
        "url_content_unavailable_note": (
            "ഈ പേജിന്റെ ഉള്ളടക്കം ലഭ്യമാക്കാൻ കഴിഞ്ഞില്ല, അതിനാൽ മുകളിലുള്ള സുരക്ഷാ പരിശോധന മാത്രമേ "
            "ലഭ്യമാകൂ — വിശ്വാസ്യതാ വിലയിരുത്തൽ സാധ്യമായില്ല."
        ),
        "url_robots_disallowed_note": (
            "ഈ സൈറ്റിന്റെ സ്വന്തം നിയമങ്ങൾ ഓട്ടോമേറ്റഡ് ടൂളുകളോട് ഈ പേജ് ആക്സസ് ചെയ്യരുതെന്ന് "
            "ആവശ്യപ്പെടുന്നു, അതിനാൽ SathyaScan ലിങ്ക് മാത്രമേ പരിശോധിച്ചുള്ളൂ — വിശ്വാസ്യതാ "
            "വിലയിരുത്തൽ സാധ്യമായില്ല."
        ),
        "unsupported_url_content": (
            "ഈ ലിങ്ക് SathyaScan-ന് വായിക്കാൻ കഴിയുന്ന ഒരു വെബ്‌പേജിലേക്കല്ല (ഇത് ഒരു ഫയൽ, ചിത്രം, "
            "അല്ലെങ്കിൽ വീഡിയോ ആയിരിക്കാം) — മുകളിലുള്ള സുരക്ഷാ പരിശോധന മാത്രമേ ലഭ്യമാകൂ."
        ),
        "url_no_claims_message": "ഈ ലിങ്കിന്റെ സുരക്ഷ ഞാൻ പരിശോധിച്ചു, പക്ഷേ പേജിൽ പരിശോധിക്കാവുന്ന ഒരു വസ്തുതാപരമായ അവകാശവാദം കണ്ടെത്താനായില്ല.",
        "analyzing_audio_ack": "🔍 നിങ്ങളുടെ വോയ്സ് സന്ദേശം വിശകലനം ചെയ്യുന്നു, ഇതിന് അല്പസമയം എടുത്തേക്കാം...",
        "audio_result_header": "🔊 *SathyaScan ഓഡിയോ ഫലം*",
        "transcript_label": "📝 *ട്രാൻസ്ക്രിപ്റ്റ്:*",
        "media_authenticity_label": "🎙️ *മീഡിയ ആധികാരികത:*",
        "transcription_failed": (
            "🎙️ ഓഡിയോ വിശ്വസനീയമായി ട്രാൻസ്ക്രൈബ് ചെയ്യാൻ എനിക്ക് കഴിഞ്ഞില്ല, അതിനാൽ അവകാശവാദം "
            "സുരക്ഷിതമായി പരിശോധിക്കാൻ കഴിയില്ല."
        ),
        "no_speech_found": "🎙️ ഈ ഓഡിയോയിൽ പരിശോധിക്കാൻ സംസാരം കണ്ടെത്താനായില്ല.",
        "audio_no_claims_message": "ഈ ഓഡിയോ ഞാൻ ട്രാൻസ്ക്രൈബ് ചെയ്തു, പക്ഷേ പരിശോധിക്കാവുന്ന ഒരു വസ്തുതാപരമായ അവകാശവാദം കണ്ടെത്താനായില്ല.",
        "duration_too_long": "ഈ ഓഡിയോ SathyaScan-ന് പ്രോസസ്സ് ചെയ്യാൻ കഴിയാത്തത്ര ദൈർഘ്യമുള്ളതാണ്. ദയവായി ചെറിയ ക്ലിപ്പ് അയയ്ക്കുക.",
        "analyzing_video_ack": "🔍 നിങ്ങളുടെ വീഡിയോ വിശകലനം ചെയ്യുന്നു, ഇതിന് അല്പസമയം എടുത്തേക്കാം...",
        "video_result_header": "🎥 *SathyaScan വീഡിയോ ഫലം*",
        "spoken_claim_label": "📝 *സംസാരിച്ച അവകാശവാദം:*",
        "on_screen_text_label": "🖼️ *വീഡിയോയിൽ കണ്ടെത്തിയ ടെക്സ്റ്റ്:*",
        "video_fact_check_status_label": "*വസ്തുത-പരിശോധന നില:*",
        "video_no_claims_message": "🎥 ഈ വീഡിയോയിൽ പരിശോധിക്കാവുന്ന ഒരു നിർദ്ദിഷ്ട വസ്തുതാപരമായ അവകാശവാദം കണ്ടെത്താനായില്ല.",
        "media_independence_note": (
            "*പ്രധാനം:*\nമീഡിയ ആധികാരികതയും വസ്തുതാ പരിശോധനയും സ്വതന്ത്ര ഫലങ്ങളാണ്."
        ),
        "video_duration_too_long": "ഈ വീഡിയോ SathyaScan-ന് പ്രോസസ്സ് ചെയ്യാൻ കഴിയാത്തത്ര ദൈർഘ്യമുള്ളതാണ്. ദയവായി ചെറിയ ക്ലിപ്പ് അയയ്ക്കുക.",
    },
    "hi": {
        "result_header": "🔍 *SathyaScan परिणाम*",
        "claim_label": "*दावा:*",
        "status_label": "*स्थिति:*",
        "why_label": "*कारण:*",
        "evidence_label": "*प्रमाण:*",
        "confidence_label": "*विश्वास स्तर:*",
        "no_evidence": "इस दावे के लिए कोई स्रोत नहीं मिला।",
        "disclaimer": (
            "_SathyaScan AI का उपयोग करता है और गलतियाँ कर सकता है। यह पूर्ण "
            "प्रमाण नहीं है — कृपया ऊपर दिए गए स्रोतों की स्वयं जाँच करें।_"
        ),
        "no_claims_message": (
            "मुझे आपके संदेश में जाँचने के लिए कोई विशिष्ट तथ्यात्मक दावा नहीं "
            "मिला। कृपया वह विशिष्ट दावा, कथन, या फॉरवर्ड किया गया संदेश भेजें "
            "जिसे आप सत्यापित करवाना चाहते हैं।"
        ),
        "confidence_high": "उच्च",
        "confidence_medium": "मध्यम",
        "confidence_low": "निम्न",
        "analyzing_ack": "🔍 आपके संदेश का विश्लेषण किया जा रहा है, इसमें थोड़ा समय लग सकता है...",
        "rate_limited": "आप संदेश थोड़ा बहुत तेज़ी से भेज रहे हैं। कृपया थोड़ा इंतज़ार करें और फिर से कोशिश करें।",
        "account_suspended_message": (
            "आपका SathyaScan खाता निलंबित कर दिया गया है। यदि आपको लगता है कि यह एक गलती है, तो कृपया "
            "सहायता से संपर्क करें।"
        ),
        "at_capacity": (
            "SathyaScan इस समय अत्यधिक मांग का सामना कर रहा है। कृपया कुछ "
            "मिनटों में फिर से कोशिश करें।"
        ),
        "processing_error": "SathyaScan इस समय इस अनुरोध को संसाधित करने में असमर्थ है। कृपया बाद में फिर से कोशिश करें।",
        "processing_timeout": "इसमें अपेक्षा से अधिक समय लग रहा है, इसलिए हमने इस प्रयास को रोक दिया है। कृपया फिर से कोशिश करें।",
        "analyzing_image_ack": "🔍 आपकी छवि का विश्लेषण किया जा रहा है, इसमें थोड़ा समय लग सकता है...",
        "content_declined": (
            "SathyaScan इस सामग्री को संसाधित करने में असमर्थ है। कृपया कुछ और "
            "भेजने का प्रयास करें।"
        ),
        "no_text_found": (
            "मुझे इस छवि में तथ्य-जाँच के लिए कोई पठनीय टेक्स्ट नहीं मिला। यदि "
            "इसमें कोई दावा है, तो कृपया टेक्स्ट सीधे भेजने का प्रयास करें।"
        ),
        "file_too_large": "यह छवि SathyaScan के संसाधित करने के लिए बहुत बड़ी है। कृपया एक छोटी छवि भेजें।",
        "unsupported_media": "SathyaScan इसे एक समर्थित छवि प्रारूप के रूप में पहचान नहीं सका।",
        "visual_analysis_unavailable_note": (
            "(नोट: यह जाँच केवल छवि में पाए गए टेक्स्ट पर आधारित है — इस "
            "अनुरोध के लिए दृश्य प्रामाणिकता विश्लेषण उपलब्ध नहीं था।)"
        ),
        "analyzing_url_ack": "🔍 यह लिंक जाँचा जा रहा है, इसमें थोड़ा समय लग सकता है...",
        "url_safety_header": "🛡️ *लिंक सुरक्षा*",
        "url_label": "*लिंक:*",
        "risk_label": "*जोखिम:*",
        "reasons_label": "*कारण:*",
        "recommendation_label": "*सिफारिश:*",
        "no_safety_concerns": "कोई विशेष चिंता नहीं मिली।",
        "url_content_unavailable_note": (
            "मैं इस पेज की सामग्री प्राप्त नहीं कर सका, इसलिए केवल ऊपर दी गई "
            "सुरक्षा जाँच ही उपलब्ध है — कोई विश्वसनीयता आकलन नहीं किया जा सका।"
        ),
        "url_robots_disallowed_note": (
            "इस साइट के अपने नियम स्वचालित उपकरणों को इस पेज तक पहुँचने से मना "
            "करते हैं, इसलिए SathyaScan ने केवल लिंक की ही जाँच की — कोई "
            "विश्वसनीयता आकलन नहीं किया जा सका।"
        ),
        "unsupported_url_content": (
            "यह लिंक किसी ऐसे वेबपेज की ओर इशारा नहीं करता जिसे SathyaScan पढ़ "
            "सके (यह एक फ़ाइल, छवि, या वीडियो हो सकता है) — केवल ऊपर दी गई "
            "सुरक्षा जाँच ही उपलब्ध है।"
        ),
        "url_no_claims_message": "मैंने इस लिंक की सुरक्षा जाँची, लेकिन पेज पर तथ्य-जाँच के लिए कोई विशिष्ट तथ्यात्मक दावा नहीं मिला।",
        "analyzing_audio_ack": "🔍 आपके वॉइस संदेश का विश्लेषण किया जा रहा है, इसमें थोड़ा समय लग सकता है...",
        "audio_result_header": "🔊 *SathyaScan ऑडियो परिणाम*",
        "transcript_label": "📝 *ट्रांसक्रिप्ट:*",
        "media_authenticity_label": "🎙️ *मीडिया प्रामाणिकता:*",
        "transcription_failed": (
            "🎙️ मैं ऑडियो को विश्वसनीय रूप से ट्रांसक्राइब नहीं कर सका, इसलिए "
            "मैं दावे को सुरक्षित रूप से सत्यापित नहीं कर सकता।"
        ),
        "no_speech_found": "🎙️ मुझे इस ऑडियो में तथ्य-जाँच के लिए कोई भाषण नहीं मिला।",
        "audio_no_claims_message": "मैंने इस ऑडियो को ट्रांसक्राइब किया, लेकिन तथ्य-जाँच के लिए कोई विशिष्ट तथ्यात्मक दावा नहीं मिला।",
        "duration_too_long": "यह ऑडियो SathyaScan के संसाधित करने के लिए बहुत लंबा है। कृपया एक छोटा क्लिप भेजें।",
        "analyzing_video_ack": "🔍 आपके वीडियो का विश्लेषण किया जा रहा है, इसमें थोड़ा समय लग सकता है...",
        "video_result_header": "🎥 *SathyaScan वीडियो परिणाम*",
        "spoken_claim_label": "📝 *बोला गया दावा:*",
        "on_screen_text_label": "🖼️ *वीडियो में मिला टेक्स्ट:*",
        "video_fact_check_status_label": "*तथ्य-जाँच स्थिति:*",
        "video_no_claims_message": "🎥 मुझे इस वीडियो में सत्यापित करने के लिए कोई विशिष्ट तथ्यात्मक दावा नहीं मिला।",
        "media_independence_note": (
            "*महत्वपूर्ण:*\nमीडिया प्रामाणिकता और तथ्यात्मक सत्यापन स्वतंत्र परिणाम हैं।"
        ),
        "video_duration_too_long": "यह वीडियो SathyaScan के संसाधित करने के लिए बहुत लंबा है। कृपया एक छोटा क्लिप भेजें।",
    },
    "ta": {
        "result_header": "🔍 *SathyaScan முடிவு*",
        "claim_label": "*கூற்று:*",
        "status_label": "*நிலை:*",
        "why_label": "*காரணம்:*",
        "evidence_label": "*சான்று:*",
        "confidence_label": "*நம்பகத்தன்மை:*",
        "no_evidence": "இந்த கூற்றுக்கு எந்த ஆதாரமும் கிடைக்கவில்லை.",
        "disclaimer": (
            "_SathyaScan AI-ஐப் பயன்படுத்துகிறது, தவறுகள் ஏற்படலாம். இது "
            "முழுமையான ஆதாரம் அல்ல — மேலே உள்ள ஆதாரங்களை நீங்களே சரிபார்க்கவும்._"
        ),
        "no_claims_message": (
            "உங்கள் செய்தியில் சரிபார்க்க குறிப்பிட்ட உண்மைக் கூற்று எதுவும் "
            "கிடைக்கவில்லை. நீங்கள் சரிபார்க்க விரும்பும் குறிப்பிட்ட கூற்று, "
            "அறிக்கை அல்லது பகிரப்பட்ட செய்தியை அனுப்பவும்."
        ),
        "confidence_high": "அதிகம்",
        "confidence_medium": "நடுத்தரம்",
        "confidence_low": "குறைவு",
        "analyzing_ack": "🔍 உங்கள் செய்தி ஆய்வு செய்யப்படுகிறது, இதற்கு சிறிது நேரம் ஆகலாம்...",
        "rate_limited": "நீங்கள் செய்திகளை சற்று வேகமாக அனுப்புகிறீர்கள். சிறிது நேரம் காத்திருந்து மீண்டும் முயற்சிக்கவும்.",
        "account_suspended_message": (
            "உங்கள் SathyaScan கணக்கு இடைநிறுத்தப்பட்டுள்ளது. இது தவறு என்று நீங்கள் நினைத்தால், "
            "ஆதரவைத் தொடர்பு கொள்ளவும்."
        ),
        "at_capacity": (
            "SathyaScan தற்போது அதிக தேவையை எதிர்கொள்கிறது. சில நிமிடங்களில் "
            "மீண்டும் முயற்சிக்கவும்."
        ),
        "processing_error": "SathyaScan தற்போது இந்த கோரிக்கையை செயலாக்க முடியவில்லை. பிறகு மீண்டும் முயற்சிக்கவும்.",
        "processing_timeout": "இது எதிர்பார்த்ததை விட அதிக நேரம் எடுக்கிறது, எனவே இந்த முயற்சியை நிறுத்திவிட்டோம். மீண்டும் முயற்சிக்கவும்.",
        "analyzing_image_ack": "🔍 உங்கள் படம் ஆய்வு செய்யப்படுகிறது, இதற்கு சிறிது நேரம் ஆகலாம்...",
        "content_declined": (
            "SathyaScan இந்த உள்ளடக்கத்தை செயலாக்க முடியவில்லை. வேறு ஏதாவது "
            "அனுப்ப முயற்சிக்கவும்."
        ),
        "no_text_found": (
            "இந்த படத்தில் உண்மை-சரிபார்ப்புக்கு படிக்கக்கூடிய எழுத்து எதுவும் "
            "கிடைக்கவில்லை. இதில் ஒரு கூற்று இருந்தால், எழுத்தை நேரடியாக அனுப்ப "
            "முயற்சிக்கவும்."
        ),
        "file_too_large": "இந்த படம் SathyaScan செயலாக்க மிகப் பெரியது. சிறிய படத்தை அனுப்பவும்.",
        "unsupported_media": "இதை ஆதரிக்கப்படும் பட வடிவமாக SathyaScan அடையாளம் காண முடியவில்லை.",
        "visual_analysis_unavailable_note": (
            "(குறிப்பு: இந்த சரிபார்ப்பு படத்தில் கண்டறியப்பட்ட எழுத்தை மட்டுமே "
            "அடிப்படையாகக் கொண்டது — இந்த கோரிக்கைக்கு காட்சி நம்பகத்தன்மை ஆய்வு "
            "கிடைக்கவில்லை.)"
        ),
        "analyzing_url_ack": "🔍 இந்த இணைப்பு சரிபார்க்கப்படுகிறது, இதற்கு சிறிது நேரம் ஆகலாம்...",
        "url_safety_header": "🛡️ *இணைப்பு பாதுகாப்பு*",
        "url_label": "*இணைப்பு:*",
        "risk_label": "*இடர்:*",
        "reasons_label": "*காரணம்:*",
        "recommendation_label": "*பரிந்துரை:*",
        "no_safety_concerns": "குறிப்பிட்ட கவலைகள் எதுவும் கண்டறியப்படவில்லை.",
        "url_content_unavailable_note": (
            "இந்தப் பக்கத்தின் உள்ளடக்கத்தை பெற முடியவில்லை, எனவே மேலே உள்ள "
            "பாதுகாப்பு சரிபார்ப்பு மட்டுமே கிடைக்கிறது — நம்பகத்தன்மை மதிப்பீடு "
            "செய்ய முடியவில்லை."
        ),
        "url_robots_disallowed_note": (
            "இந்த தளத்தின் சொந்த விதிகள் தானியங்கி கருவிகள் இந்தப் பக்கத்தை "
            "அணுகக்கூடாது என்று கூறுகின்றன, எனவே SathyaScan இணைப்பை மட்டுமே "
            "சரிபார்த்தது — நம்பகத்தன்மை மதிப்பீடு செய்ய முடியவில்லை."
        ),
        "unsupported_url_content": (
            "இந்த இணைப்பு SathyaScan படிக்கக்கூடிய இணையப்பக்கத்தை சுட்டவில்லை "
            "(இது ஒரு கோப்பு, படம் அல்லது வீடியோவாக இருக்கலாம்) — மேலே உள்ள "
            "பாதுகாப்பு சரிபார்ப்பு மட்டுமே கிடைக்கிறது."
        ),
        "url_no_claims_message": "இந்த இணைப்பின் பாதுகாப்பை சரிபார்த்தேன், ஆனால் பக்கத்தில் உண்மை-சரிபார்ப்புக்கு குறிப்பிட்ட உண்மைக் கூற்று எதுவும் கிடைக்கவில்லை.",
        "analyzing_audio_ack": "🔍 உங்கள் குரல் செய்தி ஆய்வு செய்யப்படுகிறது, இதற்கு சிறிது நேரம் ஆகலாம்...",
        "audio_result_header": "🔊 *SathyaScan ஆடியோ முடிவு*",
        "transcript_label": "📝 *படியெடுப்பு:*",
        "media_authenticity_label": "🎙️ *ஊடக நம்பகத்தன்மை:*",
        "transcription_failed": (
            "🎙️ ஆடியோவை நம்பகமாக படியெடுக்க முடியவில்லை, எனவே கூற்றை பாதுகாப்பாக "
            "சரிபார்க்க முடியாது."
        ),
        "no_speech_found": "🎙️ இந்த ஆடியோவில் உண்மை-சரிபார்ப்புக்கு பேச்சு எதுவும் கண்டறியப்படவில்லை.",
        "audio_no_claims_message": "இந்த ஆடியோவை படியெடுத்தேன், ஆனால் உண்மை-சரிபார்ப்புக்கு குறிப்பிட்ட உண்மைக் கூற்று எதுவும் கிடைக்கவில்லை.",
        "duration_too_long": "இந்த ஆடியோ SathyaScan செயலாக்க மிக நீளமானது. குறுகிய கிளிப்பை அனுப்பவும்.",
        "analyzing_video_ack": "🔍 உங்கள் வீடியோ ஆய்வு செய்யப்படுகிறது, இதற்கு சிறிது நேரம் ஆகலாம்...",
        "video_result_header": "🎥 *SathyaScan வீடியோ முடிவு*",
        "spoken_claim_label": "📝 *பேசப்பட்ட கூற்று:*",
        "on_screen_text_label": "🖼️ *வீடியோவில் கண்டறியப்பட்ட எழுத்து:*",
        "video_fact_check_status_label": "*உண்மை-சரிபார்ப்பு நிலை:*",
        "video_no_claims_message": "🎥 இந்த வீடியோவில் சரிபார்க்க குறிப்பிட்ட உண்மைக் கூற்று எதுவும் கிடைக்கவில்லை.",
        "media_independence_note": (
            "*முக்கியம்:*\nஊடக நம்பகத்தன்மை மற்றும் உண்மைச் சரிபார்ப்பு ஆகியவை சுயாதீன முடிவுகள்."
        ),
        "video_duration_too_long": "இந்த வீடியோ SathyaScan செயலாக்க மிக நீளமானது. குறுகிய கிளிப்பை அனுப்பவும்.",
    },
}


# ==== Phase 4 (URL pipeline) ====

RISK_LEVEL_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "safe": "🟢 Safe",
        "low": "🟡 Low Risk",
        "medium": "🟠 Medium Risk",
        "high": "🔴 High Risk",
        "critical": "🔴 Critical Risk",
    },
    "ml": {
        "safe": "🟢 സുരക്ഷിതം",
        "low": "🟡 കുറഞ്ഞ അപകടസാധ്യത",
        "medium": "🟠 ഇടത്തരം അപകടസാധ്യത",
        "high": "🔴 ഉയർന്ന അപകടസാധ്യത",
        "critical": "🔴 ഗുരുതരമായ അപകടസാധ്യത",
    },
    "hi": {
        "safe": "🟢 सुरक्षित",
        "low": "🟡 कम जोखिम",
        "medium": "🟠 मध्यम जोखिम",
        "high": "🔴 उच्च जोखिम",
        "critical": "🔴 गंभीर जोखिम",
    },
    "ta": {
        "safe": "🟢 பாதுகாப்பானது",
        "low": "🟡 குறைந்த இடர்",
        "medium": "🟠 நடுத்தர இடர்",
        "high": "🔴 அதிக இடர்",
        "critical": "🔴 கடுமையான இடர்",
    },
}

# Keyed by app.agent.tools.url_safety.FindingCode values. `{param}` markers
# match the `params` dict each Finding carries — see
# app/agent/tools/response_generation.py's rendering (a missing/extra param
# never raises: falls back to the raw template on a format() error).
URL_SAFETY_FINDING_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "private_address_blocked": "This link points to a private or internal network address ({host}) and was not fetched.",
        "missing_https": "This link does not use a secure (HTTPS) connection.",
        "ip_address_as_host": "This link uses a raw IP address instead of a domain name.",
        "punycode_domain": "This link's domain uses characters that can visually impersonate another domain.",
        "suspicious_domain_pattern": "This domain's name resembles a well-known brand or uses a suspicious pattern.",
        "excessive_subdomains": "This link uses an unusually large number of subdomains.",
        "redirect_crosses_domains": "This link redirects to a different domain than the one you sent.",
        "many_redirects": "This link redirects through {hop_count} hops before reaching its destination.",
        "phishing_keywords": "This page uses urgent or account-verification language often seen in phishing ({keywords}).",
        "fake_login_form": "This page contains a login/password form — be cautious before entering any credentials.",
        "young_domain": "This domain was registered only {age_days} day(s) ago.",
        "threat_intel_match": "This link matched {count} known threat-intelligence report(s).",
    },
    "ml": {
        "private_address_blocked": "ഈ ലിങ്ക് സ്വകാര്യ അല്ലെങ്കിൽ ആന്തരിക നെറ്റ്‌വർക്ക് വിലാസത്തിലേക്ക് ({host}) ചൂണ്ടിക്കാണിക്കുന്നു, ഇത് ലഭ്യമാക്കിയില്ല.",
        "missing_https": "ഈ ലിങ്ക് സുരക്ഷിതമായ (HTTPS) കണക്ഷൻ ഉപയോഗിക്കുന്നില്ല.",
        "ip_address_as_host": "ഈ ലിങ്ക് ഒരു ഡൊമെയ്ൻ നാമത്തിന് പകരം IP വിലാസം ഉപയോഗിക്കുന്നു.",
        "punycode_domain": "ഈ ലിങ്കിന്റെ ഡൊമെയ്ൻ മറ്റൊരു ഡൊമെയ്നിനെ അനുകരിക്കാൻ കഴിയുന്ന പ്രതീകങ്ങൾ ഉപയോഗിക്കുന്നു.",
        "suspicious_domain_pattern": "ഈ ഡൊമെയ്‌ന്റെ പേര് അറിയപ്പെടുന്ന ഒരു ബ്രാൻഡിനോട് സാമ്യമുള്ളതോ സംശയാസ്പദമായ പാറ്റേൺ ഉപയോഗിക്കുന്നതോ ആണ്.",
        "excessive_subdomains": "ഈ ലിങ്ക് അസാധാരണമായി അധികം സബ്ഡൊമെയ്‌നുകൾ ഉപയോഗിക്കുന്നു.",
        "redirect_crosses_domains": "നിങ്ങൾ അയച്ച ഡൊമെയ്‌നിൽ നിന്ന് വ്യത്യസ്തമായ ഒന്നിലേക്ക് ഈ ലിങ്ക് റീഡയറക്ട് ചെയ്യുന്നു.",
        "many_redirects": "ലക്ഷ്യസ്ഥാനത്ത് എത്തുന്നതിന് മുമ്പ് ഈ ലിങ്ക് {hop_count} ഘട്ടങ്ങളിലൂടെ റീഡയറക്ട് ചെയ്യുന്നു.",
        "phishing_keywords": "ഫിഷിംഗിൽ സാധാരണ കാണുന്ന അടിയന്തിര അല്ലെങ്കിൽ അക്കൗണ്ട്-സ്ഥിരീകരണ ഭാഷ ഈ പേജ് ഉപയോഗിക്കുന്നു ({keywords}).",
        "fake_login_form": "ഈ പേജിൽ ലോഗിൻ/പാസ്‌വേഡ് ഫോം ഉണ്ട് — ഏതെങ്കിലും ക്രെഡൻഷ്യലുകൾ നൽകുന്നതിന് മുമ്പ് ജാഗ്രത പാലിക്കുക.",
        "young_domain": "ഈ ഡൊമെയ്ൻ {age_days} ദിവസം മുമ്പ് മാത്രമാണ് രജിസ്റ്റർ ചെയ്തത്.",
        "threat_intel_match": "ഈ ലിങ്ക് {count} അറിയപ്പെടുന്ന ഭീഷണി-ഇന്റലിജൻസ് റിപ്പോർട്ട(കൾ) മായി പൊരുത്തപ്പെട്ടു.",
    },
    "hi": {
        "private_address_blocked": "यह लिंक एक निजी या आंतरिक नेटवर्क पते ({host}) की ओर इशारा करता है और इसे प्राप्त नहीं किया गया।",
        "missing_https": "यह लिंक सुरक्षित (HTTPS) कनेक्शन का उपयोग नहीं करता।",
        "ip_address_as_host": "यह लिंक डोमेन नाम के बजाय एक कच्चे IP पते का उपयोग करता है।",
        "punycode_domain": "इस लिंक का डोमेन ऐसे वर्णों का उपयोग करता है जो दृष्टिगत रूप से किसी अन्य डोमेन की नकल कर सकते हैं।",
        "suspicious_domain_pattern": "इस डोमेन का नाम किसी प्रसिद्ध ब्रांड जैसा दिखता है या संदिग्ध पैटर्न का उपयोग करता है।",
        "excessive_subdomains": "यह लिंक असामान्य रूप से बड़ी संख्या में सबडोमेन का उपयोग करता है।",
        "redirect_crosses_domains": "यह लिंक आपके भेजे गए डोमेन से अलग डोमेन पर रीडायरेक्ट करता है।",
        "many_redirects": "यह लिंक अपने गंतव्य तक पहुँचने से पहले {hop_count} चरणों से रीडायरेक्ट होता है।",
        "phishing_keywords": "यह पेज फिशिंग में आमतौर पर देखी जाने वाली तत्काल या खाता-सत्यापन भाषा का उपयोग करता है ({keywords})।",
        "fake_login_form": "इस पेज में लॉगिन/पासवर्ड फ़ॉर्म है — कोई भी क्रेडेंशियल दर्ज करने से पहले सावधान रहें।",
        "young_domain": "यह डोमेन केवल {age_days} दिन पहले पंजीकृत किया गया था।",
        "threat_intel_match": "यह लिंक {count} ज्ञात खतरा-खुफिया रिपोर्ट(ओं) से मेल खाता है।",
    },
    "ta": {
        "private_address_blocked": "இந்த இணைப்பு தனிப்பட்ட அல்லது உள் நெட்வொர்க் முகவரியை ({host}) சுட்டுகிறது, எனவே அது பெறப்படவில்லை.",
        "missing_https": "இந்த இணைப்பு பாதுகாப்பான (HTTPS) இணைப்பைப் பயன்படுத்தவில்லை.",
        "ip_address_as_host": "இந்த இணைப்பு டொமைன் பெயருக்குப் பதிலாக நேரடி IP முகவரியைப் பயன்படுத்துகிறது.",
        "punycode_domain": "இந்த இணைப்பின் டொமைன் மற்றொரு டொமைனைப் போல தோற்றமளிக்கும் எழுத்துக்களைப் பயன்படுத்துகிறது.",
        "suspicious_domain_pattern": "இந்த டொமைனின் பெயர் நன்கு அறியப்பட்ட பிராண்டை ஒத்திருக்கிறது அல்லது சந்தேகத்திற்குரிய முறையைப் பயன்படுத்துகிறது.",
        "excessive_subdomains": "இந்த இணைப்பு அசாதாரணமாக அதிக எண்ணிக்கையிலான துணை-டொமைன்களைப் பயன்படுத்துகிறது.",
        "redirect_crosses_domains": "நீங்கள் அனுப்பிய டொமைனில் இருந்து வேறு டொமைனுக்கு இந்த இணைப்பு திருப்பி விடுகிறது.",
        "many_redirects": "இலக்கை அடைவதற்கு முன் இந்த இணைப்பு {hop_count} கட்டங்கள் வழியாக திருப்பி விடப்படுகிறது.",
        "phishing_keywords": "ஃபிஷிங்கில் பொதுவாகக் காணப்படும் அவசர அல்லது கணக்கு-சரிபார்ப்பு மொழியை இந்தப் பக்கம் பயன்படுத்துகிறது ({keywords}).",
        "fake_login_form": "இந்தப் பக்கத்தில் உள்நுழைவு/கடவுச்சொல் படிவம் உள்ளது — எந்த சான்றுகளையும் உள்ளிடும் முன் எச்சரிக்கையாக இருக்கவும்.",
        "young_domain": "இந்த டொமைன் {age_days} நாட்களுக்கு முன்பு மட்டுமே பதிவு செய்யப்பட்டது.",
        "threat_intel_match": "இந்த இணைப்பு {count} அறியப்பட்ட அச்சுறுத்தல்-நுண்ணறிவு அறிக்கை(கள்) உடன் பொருந்துகிறது.",
    },
}

URL_SAFETY_RECOMMENDATIONS: dict[str, dict[str, str]] = {
    "en": {
        "safe": "No obvious safety concerns were found. Still use your own judgment before entering any personal information.",
        "low": "No major concerns found, but stay cautious — avoid entering personal information unless you trust the source.",
        "medium": "Exercise caution with this link. Avoid entering any personal or financial information.",
        "high": (
            "This link shows signs of being unsafe. Do not enter passwords or financial information, and avoid "
            "downloading anything from it."
        ),
        "critical": (
            "This link appears dangerous. Do not open it, and do not enter any personal, password, or financial "
            "information."
        ),
    },
    "ml": {
        "safe": (
            "പ്രകടമായ സുരക്ഷാ ആശങ്കകളൊന്നും കണ്ടെത്തിയില്ല. എന്നിരുന്നാലും ഏതെങ്കിലും വ്യക്തിഗത വിവരങ്ങൾ "
            "നൽകുന്നതിന് മുമ്പ് സ്വന്തം വിവേചനാധികാരം ഉപയോഗിക്കുക."
        ),
        "low": (
            "വലിയ ആശങ്കകളൊന്നും കണ്ടെത്തിയില്ല, എന്നാൽ ജാഗ്രത പാലിക്കുക — ഉറവിടത്തെ വിശ്വസിക്കുന്നില്ലെങ്കിൽ "
            "വ്യക്തിഗത വിവരങ്ങൾ നൽകരുത്."
        ),
        "medium": "ഈ ലിങ്കിനൊപ്പം ജാഗ്രത പാലിക്കുക. വ്യക്തിഗതമോ സാമ്പത്തികമോ ആയ വിവരങ്ങളൊന്നും നൽകരുത്.",
        "high": (
            "ഈ ലിങ്ക് സുരക്ഷിതമല്ലാത്തതിന്റെ ലക്ഷണങ്ങൾ കാണിക്കുന്നു. പാസ്‌വേഡുകളോ സാമ്പത്തിക വിവരങ്ങളോ "
            "നൽകരുത്, ഇതിൽ നിന്ന് ഒന്നും ഡൗൺലോഡ് ചെയ്യരുത്."
        ),
        "critical": (
            "ഈ ലിങ്ക് അപകടകരമാണെന്ന് തോന്നുന്നു. ഇത് തുറക്കരുത്, വ്യക്തിഗതമോ പാസ്‌വേഡോ സാമ്പത്തിക "
            "വിവരങ്ങളോ നൽകരുത്."
        ),
    },
    "hi": {
        "safe": (
            "कोई स्पष्ट सुरक्षा चिंता नहीं मिली। फिर भी कोई भी व्यक्तिगत जानकारी दर्ज करने से पहले अपने "
            "स्वयं के विवेक का उपयोग करें।"
        ),
        "low": (
            "कोई बड़ी चिंता नहीं मिली, लेकिन सतर्क रहें — जब तक आप स्रोत पर भरोसा न करें, व्यक्तिगत "
            "जानकारी दर्ज करने से बचें।"
        ),
        "medium": "इस लिंक के साथ सावधानी बरतें। कोई भी व्यक्तिगत या वित्तीय जानकारी दर्ज करने से बचें।",
        "high": (
            "यह लिंक असुरक्षित होने के संकेत दिखाता है। पासवर्ड या वित्तीय जानकारी दर्ज न करें, और इससे "
            "कुछ भी डाउनलोड करने से बचें।"
        ),
        "critical": (
            "यह लिंक खतरनाक प्रतीत होता है। इसे न खोलें, और कोई भी व्यक्तिगत, पासवर्ड, या वित्तीय जानकारी "
            "दर्ज न करें।"
        ),
    },
    "ta": {
        "safe": (
            "வெளிப்படையான பாதுகாப்பு கவலைகள் எதுவும் கண்டறியப்படவில்லை. எனினும் எந்த தனிப்பட்ட "
            "தகவலையும் உள்ளிடும் முன் உங்கள் சொந்த விவேகத்தைப் பயன்படுத்தவும்."
        ),
        "low": (
            "பெரிய கவலைகள் எதுவும் கண்டறியப்படவில்லை, ஆனால் எச்சரிக்கையாக இருங்கள் — மூலத்தை நம்பாத "
            "வரை தனிப்பட்ட தகவலை உள்ளிடுவதைத் தவிர்க்கவும்."
        ),
        "medium": "இந்த இணைப்புடன் எச்சரிக்கையாக இருங்கள். தனிப்பட்ட அல்லது நிதி தகவல்களை உள்ளிடுவதைத் தவிர்க்கவும்.",
        "high": (
            "இந்த இணைப்பு பாதுகாப்பற்றதாக இருப்பதற்கான அறிகுறிகளைக் காட்டுகிறது. கடவுச்சொற்கள் அல்லது "
            "நிதித் தகவல்களை உள்ளிட வேண்டாம், மேலும் இதிலிருந்து எதையும் பதிவிறக்குவதைத் தவிர்க்கவும்."
        ),
        "critical": (
            "இந்த இணைப்பு ஆபத்தானதாகத் தெரிகிறது. இதைத் திறக்க வேண்டாம், மேலும் எந்த தனிப்பட்ட, "
            "கடவுச்சொல் அல்லது நிதித் தகவலையும் உள்ளிட வேண்டாம்."
        ),
    },
}


# ==== Phase 5 (Audio/Video pipelines) ====

# Keyed by app.agent.tools.audio_forensics.AudioForensicsStatus /
# app.agent.tools.video_forensics.VideoForensicsStatus values — both tools
# share the same status vocabulary (AI_GENERATED_LIKELY/UNLIKELY,
# MANIPULATED_LIKELY, AUTHENTICITY_UNCERTAIN, PROVIDER_UNAVAILABLE, FAILED),
# so one label set covers both. PROVIDER_UNAVAILABLE's English string is
# fixed verbatim per the user's explicit Phase 5 instruction: "AI-generation
# detection unavailable" — never a guess, never omitted. FAILED renders
# identically to PROVIDER_UNAVAILABLE from the user's perspective (an
# internal wrapper failure is not information the user can act on
# differently than "no reliable answer available").
MEDIA_FORENSICS_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "ai_generated_likely": "⚠️ AI-generated/manipulated media likely",
        "ai_generated_unlikely": "✅ No strong signs of AI generation detected",
        "manipulated_likely": "⚠️ This media may have been manipulated",
        "authenticity_uncertain": "❓ Authenticity uncertain — not enough signal either way",
        "provider_unavailable": "⚠️ AI-generation detection unavailable",
        "failed": "⚠️ AI-generation detection unavailable",
    },
    "ml": {
        "ai_generated_likely": "⚠️ AI-ജനറേറ്റഡ്/കൃത്രിമമായി മാറ്റം വരുത്തിയ മീഡിയ ആകാൻ സാധ്യത",
        "ai_generated_unlikely": "✅ AI ജനറേഷന്റെ ശക്തമായ ലക്ഷണങ്ങളൊന്നും കണ്ടെത്തിയില്ല",
        "manipulated_likely": "⚠️ ഈ മീഡിയയിൽ കൃത്രിമം കാണിച്ചിരിക്കാം",
        "authenticity_uncertain": "❓ ആധികാരികത അനിശ്ചിതം — ഇരുവശത്തേക്കും മതിയായ സൂചനയില്ല",
        "provider_unavailable": "⚠️ AI-ജനറേഷൻ കണ്ടെത്തൽ ലഭ്യമല്ല",
        "failed": "⚠️ AI-ജനറേഷൻ കണ്ടെത്തൽ ലഭ്യമല്ല",
    },
    "hi": {
        "ai_generated_likely": "⚠️ AI-जनित/परिवर्तित मीडिया होने की संभावना",
        "ai_generated_unlikely": "✅ AI जनरेशन के कोई मजबूत संकेत नहीं मिले",
        "manipulated_likely": "⚠️ यह मीडिया परिवर्तित किया गया हो सकता है",
        "authenticity_uncertain": "❓ प्रामाणिकता अनिश्चित — किसी भी दिशा में पर्याप्त संकेत नहीं",
        "provider_unavailable": "⚠️ AI-जनरेशन का पता लगाना अनुपलब्ध है",
        "failed": "⚠️ AI-जनरेशन का पता लगाना अनुपलब्ध है",
    },
    "ta": {
        "ai_generated_likely": "⚠️ AI-உருவாக்கிய/திருத்தப்பட்ட ஊடகமாக இருக்க வாய்ப்புள்ளது",
        "ai_generated_unlikely": "✅ AI உருவாக்கத்தின் வலுவான அறிகுறிகள் எதுவும் கண்டறியப்படவில்லை",
        "manipulated_likely": "⚠️ இந்த ஊடகம் திருத்தப்பட்டிருக்கலாம்",
        "authenticity_uncertain": "❓ நம்பகத்தன்மை நிச்சயமற்றது — எந்தப் பக்கத்திற்கும் போதிய குறிப்பு இல்லை",
        "provider_unavailable": "⚠️ AI-உருவாக்கம் கண்டறிதல் கிடைக்கவில்லை",
        "failed": "⚠️ AI-உருவாக்கம் கண்டறிதல் கிடைக்கவில்லை",
    },
}


# ==== Phase 8 (notifications, app/agent/notifications.py) ====
# {claim} / {old_result} / {new_result} placeholders are filled via
# str.format() by the caller — never an LLM call, same "deterministic
# template, not free text" precedent as every other string in this module.
NOTIFICATION_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "scheduled_check_completed_title": "Re-check complete",
        "scheduled_check_completed_body_unchanged": (
            'Your scheduled re-check on "{claim}" is complete — the result is still {new_result}.'
        ),
        "credibility_changed_title": "Result changed on re-check",
        "credibility_changed_body": (
            'Your scheduled re-check on "{claim}" found a different result: {old_result} → {new_result}.'
        ),
        "scheduled_check_failed_title": "Re-check failed",
        "scheduled_check_failed_body": 'We were unable to complete your scheduled re-check on "{claim}".',
        "security_event_title": "Security notice",
        "account_deleted_title": "Account deleted",
        "account_deleted_body": "Your SathyaScan account and associated data have been deleted, as you requested.",
    },
    "ml": {
        "scheduled_check_completed_title": "വീണ്ടും പരിശോധന പൂർത്തിയായി",
        "scheduled_check_completed_body_unchanged": (
            '"{claim}" എന്നതിനെക്കുറിച്ചുള്ള നിങ്ങളുടെ വീണ്ടും പരിശോധന പൂർത്തിയായി — ഫലം ഇപ്പോഴും {new_result} ആണ്.'
        ),
        "credibility_changed_title": "വീണ്ടും പരിശോധനയിൽ ഫലം മാറി",
        "credibility_changed_body": (
            '"{claim}" എന്നതിനെക്കുറിച്ചുള്ള നിങ്ങളുടെ വീണ്ടും പരിശോധനയിൽ വ്യത്യസ്തമായ ഫലം കണ്ടെത്തി: {old_result} → {new_result}.'
        ),
        "scheduled_check_failed_title": "വീണ്ടും പരിശോധന പരാജയപ്പെട്ടു",
        "scheduled_check_failed_body": '"{claim}" എന്നതിനെക്കുറിച്ചുള്ള നിങ്ങളുടെ ഷെഡ്യൂൾ ചെയ്ത വീണ്ടും പരിശോധന പൂർത്തിയാക്കാൻ ഞങ്ങൾക്ക് കഴിഞ്ഞില്ല.',
        "security_event_title": "സുരക്ഷാ അറിയിപ്പ്",
        "account_deleted_title": "അക്കൗണ്ട് ഇല്ലാതാക്കി",
        "account_deleted_body": "നിങ്ങൾ അഭ്യർത്ഥിച്ചതുപോലെ, നിങ്ങളുടെ SathyaScan അക്കൗണ്ടും അനുബന്ധ ഡാറ്റയും ഇല്ലാതാക്കി.",
    },
    "hi": {
        "scheduled_check_completed_title": "पुनः जाँच पूर्ण",
        "scheduled_check_completed_body_unchanged": (
            '"{claim}" पर आपकी अनुसूचित पुनः जाँच पूर्ण हो गई है — परिणाम अभी भी {new_result} है।'
        ),
        "credibility_changed_title": "पुनः जाँच में परिणाम बदला",
        "credibility_changed_body": (
            '"{claim}" पर आपकी अनुसूचित पुनः जाँच में एक अलग परिणाम मिला: {old_result} → {new_result}।'
        ),
        "scheduled_check_failed_title": "पुनः जाँच विफल",
        "scheduled_check_failed_body": '"{claim}" पर आपकी अनुसूचित पुनः जाँच पूरी करने में हम असमर्थ रहे।',
        "security_event_title": "सुरक्षा सूचना",
        "account_deleted_title": "खाता हटाया गया",
        "account_deleted_body": "आपके अनुरोध के अनुसार, आपका SathyaScan खाता और संबंधित डेटा हटा दिया गया है।",
    },
    "ta": {
        "scheduled_check_completed_title": "மறு-சரிபார்ப்பு முடிந்தது",
        "scheduled_check_completed_body_unchanged": (
            '"{claim}" குறித்த உங்கள் திட்டமிடப்பட்ட மறு-சரிபார்ப்பு முடிந்தது — முடிவு இன்னும் {new_result} ஆக உள்ளது.'
        ),
        "credibility_changed_title": "மறு-சரிபார்ப்பில் முடிவு மாறியது",
        "credibility_changed_body": (
            '"{claim}" குறித்த உங்கள் திட்டமிடப்பட்ட மறு-சரிபார்ப்பில் வேறு முடிவு கண்டறியப்பட்டது: {old_result} → {new_result}.'
        ),
        "scheduled_check_failed_title": "மறு-சரிபார்ப்பு தோல்வியடைந்தது",
        "scheduled_check_failed_body": '"{claim}" குறித்த உங்கள் திட்டமிடப்பட்ட மறு-சரிபார்ப்பை முடிக்க முடியவில்லை.',
        "security_event_title": "பாதுகாப்பு அறிவிப்பு",
        "account_deleted_title": "கணக்கு நீக்கப்பட்டது",
        "account_deleted_body": "நீங்கள் கோரியபடி, உங்கள் SathyaScan கணக்கு மற்றும் தொடர்புடைய தரவு நீக்கப்பட்டுள்ளது.",
    },
}


# ==== Phase 8 (PDF reports, app/agent/pdf_report.py) ====
# Full en/ml/hi/ta support, matching every other dict in this module. The
# fix that closed the "hi/ta fall back to English" gap (see this file's
# module docstring) ran alongside app/agent/pdf_report.py's Unicode-font
# rewrite: the "encoding_note" text below now describes genuinely-missing
# glyphs (rare, e.g. a third script mixed into a claim) rather than the old
# "any non-Latin script renders as '?'" blanket caveat.
PDF_REPORT_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "SathyaScan Fact-Check Report",
        "subtitle": "AI-assisted, evidence-based fact-checking",
        "input_type_label": "Input type:",
        "language_label": "Language:",
        "checked_at_label": "Checked at:",
        "result_label": "Result:",
        "claim_label": "Claim:",
        "credibility_score_label": "Credibility score:",
        "explanation_label": "Explanation:",
        "note_label": "Note:",
        "incomplete_investigation_note": (
            "This investigation did not complete (reason: {reason}). This is not a verdict either way."
        ),
        "evidence_sources_label": "Evidence & Sources:",
        "no_sources_found": "No sources were found for this claim.",
        "no_claims_found": "No factual claims were identified in this analysis.",
        "disclaimer": (
            "SathyaScan uses AI and may make mistakes. This report is not absolute proof. "
            "Please verify the sources listed below yourself before relying on this result."
        ),
        "encoding_note": (
            "Note: a small number of characters in this report could not be displayed with the fonts "
            "currently available and are shown as '?'."
        ),
    },
    "ml": {
        "title": "SathyaScan വസ്തുത പരിശോധന റിപ്പോർട്ട്",
        "subtitle": "AI-സഹായത്തോടെയുള്ള, തെളിവ് അടിസ്ഥാനമാക്കിയുള്ള വസ്തുത പരിശോധന",
        "input_type_label": "ഇൻപുട്ട് തരം:",
        "language_label": "ഭാഷ:",
        "checked_at_label": "പരിശോധിച്ച സമയം:",
        "result_label": "ഫലം:",
        "claim_label": "അവകാശവാദം:",
        "credibility_score_label": "വിശ്വാസ്യത സ്കോർ:",
        "explanation_label": "വിശദീകരണം:",
        "note_label": "കുറിപ്പ്:",
        "incomplete_investigation_note": (
            "ഈ അന്വേഷണം പൂർത്തിയായില്ല (കാരണം: {reason}). ഇത് ഏതെങ്കിലും വിധത്തിലുള്ള വിധിയല്ല."
        ),
        "evidence_sources_label": "തെളിവുകളും ഉറവിടങ്ങളും:",
        "no_sources_found": "ഈ അവകാശവാദത്തിന് ഉറവിടങ്ങളൊന്നും കണ്ടെത്തിയില്ല.",
        "no_claims_found": "ഈ വിശകലനത്തിൽ പരിശോധിക്കാവുന്ന വസ്തുതാപരമായ അവകാശവാദങ്ങളൊന്നും കണ്ടെത്തിയില്ല.",
        "disclaimer": (
            "SathyaScan AI ഉപയോഗിക്കുന്നു, തെറ്റുകൾ സംഭവിക്കാം. ഈ റിപ്പോർട്ട് സമ്പൂർണ്ണ തെളിവല്ല. "
            "ഇത് ആശ്രയിക്കുന്നതിന് മുമ്പ് ചുവടെയുള്ള ഉറവിടങ്ങൾ സ്വയം പരിശോധിക്കുക."
        ),
        "encoding_note": (
            "കുറിപ്പ്: ഈ റിപ്പോർട്ടിലെ ചില പ്രതീകങ്ങൾ നിലവിൽ ലഭ്യമായ ഫോണ്ടുകൾ ഉപയോഗിച്ച് പ്രദർശിപ്പിക്കാൻ "
            "കഴിഞ്ഞില്ല, അവ '?' ആയി കാണിച്ചിരിക്കുന്നു."
        ),
    },
    "hi": {
        "title": "SathyaScan तथ्य-जाँच रिपोर्ट",
        "subtitle": "AI-सहायता प्राप्त, प्रमाण-आधारित तथ्य-जाँच",
        "input_type_label": "इनपुट प्रकार:",
        "language_label": "भाषा:",
        "checked_at_label": "जाँचा गया समय:",
        "result_label": "परिणाम:",
        "claim_label": "दावा:",
        "credibility_score_label": "विश्वसनीयता स्कोर:",
        "explanation_label": "स्पष्टीकरण:",
        "note_label": "नोट:",
        "incomplete_investigation_note": (
            "यह जाँच पूरी नहीं हुई (कारण: {reason})। यह किसी भी तरह से एक फैसला नहीं है।"
        ),
        "evidence_sources_label": "प्रमाण और स्रोत:",
        "no_sources_found": "इस दावे के लिए कोई स्रोत नहीं मिला।",
        "no_claims_found": "इस विश्लेषण में कोई तथ्यात्मक दावा नहीं मिला।",
        "disclaimer": (
            "SathyaScan AI का उपयोग करता है और गलतियाँ कर सकता है। यह रिपोर्ट पूर्ण प्रमाण नहीं है। "
            "इस परिणाम पर भरोसा करने से पहले कृपया नीचे दिए गए स्रोतों की स्वयं जाँच करें।"
        ),
        "encoding_note": (
            "नोट: इस रिपोर्ट में कुछ वर्ण वर्तमान में उपलब्ध फोंट के साथ प्रदर्शित नहीं किए जा सके और "
            "उन्हें '?' के रूप में दिखाया गया है।"
        ),
    },
    "ta": {
        "title": "SathyaScan உண்மை-சரிபார்ப்பு அறிக்கை",
        "subtitle": "AI-உதவியுடன், ஆதாரம் அடிப்படையிலான உண்மை-சரிபார்ப்பு",
        "input_type_label": "உள்ளீட்டு வகை:",
        "language_label": "மொழி:",
        "checked_at_label": "சரிபார்க்கப்பட்ட நேரம்:",
        "result_label": "முடிவு:",
        "claim_label": "கூற்று:",
        "credibility_score_label": "நம்பகத்தன்மை மதிப்பெண்:",
        "explanation_label": "விளக்கம்:",
        "note_label": "குறிப்பு:",
        "incomplete_investigation_note": (
            "இந்த விசாரணை முடிக்கப்படவில்லை (காரணம்: {reason}). இது எந்த வகையிலும் ஒரு தீர்ப்பு அல்ல."
        ),
        "evidence_sources_label": "சான்றுகள் & ஆதாரங்கள்:",
        "no_sources_found": "இந்த கூற்றுக்கு எந்த ஆதாரமும் கிடைக்கவில்லை.",
        "no_claims_found": "இந்த பகுப்பாய்வில் உண்மைச் சார்ந்த கூற்றுகள் எதுவும் கண்டறியப்படவில்லை.",
        "disclaimer": (
            "SathyaScan AI-ஐப் பயன்படுத்துகிறது, தவறுகள் ஏற்படலாம். இந்த அறிக்கை முழுமையான ஆதாரம் அல்ல. "
            "இந்த முடிவை நம்புவதற்கு முன் கீழே பட்டியலிடப்பட்டுள்ள ஆதாரங்களை நீங்களே சரிபார்க்கவும்."
        ),
        "encoding_note": (
            "குறிப்பு: இந்த அறிக்கையில் சில எழுத்துக்களை தற்போது கிடைக்கும் எழுத்துருக்களால் காட்ட "
            "முடியவில்லை, அவை '?' எனக் காட்டப்பட்டுள்ளன."
        ),
    },
}


def resolve_language(requested: str | None) -> str:
    if requested and requested in SUPPORTED_LANGUAGES:
        return requested
    return DEFAULT_LANGUAGE
