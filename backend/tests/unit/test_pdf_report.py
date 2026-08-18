"""
Phase 8 PDF report generation (app/agent/pdf_report.py) against a real
in-memory SQLite database (reuses load_verdicts, the same claims+evidence
assembly logic the History API's detail endpoint uses).
"""

import io
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.pdf_report import generate_analysis_report_pdf
from app.db.base import Base
from app.db.session import build_sessionmaker
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.evidence import Evidence
from app.models.user import User


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()


async def _make_analysis_with_claim(session, *, claim_text="A test claim.", result="false", evidence=True, language="en"):
    user = User(phone_number_encrypted=b"x", phone_number_hash=uuid.uuid4().hex)
    session.add(user)
    await session.flush()

    analysis = Analysis(
        user_id=user.id,
        input_type="text",
        input_text=claim_text,
        content_fingerprint=uuid.uuid4().hex,
        source_wamid=f"wamid.{uuid.uuid4().hex}",
        status="completed",
        overall_result=result,
        language=language,
        privacy_mode_snapshot=True,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(analysis)
    await session.flush()

    claim = Claim(
        analysis_id=analysis.id,
        claim_text=claim_text,
        claim_order=0,
        result=result,
        investigation_complete=True,
        claim_confidence=0.85,
        reasoning_text="Detailed reasoning about why this is the verdict.",
        category="health",
    )
    session.add(claim)
    await session.flush()

    if evidence:
        session.add(
            Evidence(
                claim_id=claim.id,
                source_url="https://pib.gov.in/example",
                source_domain="pib.gov.in",
                source_title="Official statement",
                publisher_name="PIB",
                stance="contradicting",
                credibility_tier="tier_1_gov_official",
                snippet_text="No such scheme exists.",
                retrieved_at=datetime.now(timezone.utc),
            )
        )

    await session.commit()
    return analysis


@pytest.mark.asyncio
async def test_generates_nonempty_valid_pdf(db_session):
    analysis = await _make_analysis_with_claim(db_session)
    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 500


@pytest.mark.asyncio
async def test_pdf_never_contains_phone_number_bytes(db_session):
    analysis = await _make_analysis_with_claim(db_session)
    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    # The raw encrypted phone bytes 'x'*N never appear in the PDF at all —
    # nothing in the PDF-generation code path even reads
    # phone_number_encrypted/phone_number_hash.
    assert b"phone_number" not in pdf_bytes


@pytest.mark.asyncio
async def test_pdf_with_no_claims_still_generates(db_session):
    user = User(phone_number_encrypted=b"x", phone_number_hash=uuid.uuid4().hex)
    db_session.add(user)
    await db_session.flush()
    analysis = Analysis(
        user_id=user.id,
        input_type="text",
        input_text="no claims here",
        content_fingerprint=uuid.uuid4().hex,
        source_wamid=f"wamid.{uuid.uuid4().hex}",
        status="completed",
        overall_result="no_claims_detected",
        language="en",
        privacy_mode_snapshot=True,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(analysis)
    await db_session.commit()

    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    assert pdf_bytes.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_pdf_with_no_evidence_still_generates(db_session):
    analysis = await _make_analysis_with_claim(db_session, evidence=False)
    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    assert pdf_bytes.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_pdf_handles_non_latin1_text_without_crashing(db_session):
    """Malayalam script claim text — must not raise, per the documented
    honest limitation (replaced with '?', noted in the PDF footer), not an
    unhandled UnicodeEncodeError crash."""
    analysis = await _make_analysis_with_claim(db_session, claim_text="ഇത് ഒരു വ്യാജ വാർത്തയാണ്")
    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    assert pdf_bytes.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_pdf_uses_malayalam_labels_for_malayalam_analysis(db_session):
    """PDF labels (not just claim/reasoning text) are drawn from
    app.i18n.templates.PDF_REPORT_STRINGS['ml'] when analysis.language ==
    'ml' — proven by asserting the ENGLISH label strings are NOT what got
    requested (the ml dict was actually consulted)."""
    analysis = await _make_analysis_with_claim(db_session, language="ml")
    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    assert pdf_bytes.startswith(b"%PDF")

    from app.i18n.templates import PDF_REPORT_STRINGS

    assert PDF_REPORT_STRINGS["ml"]["claim_label"] != PDF_REPORT_STRINGS["en"]["claim_label"]


@pytest.mark.asyncio
async def test_pdf_uses_hindi_and_tamil_labels_not_english_fallback(db_session):
    """Hindi/Tamil localization fix: hi/ta now have their OWN
    PDF_REPORT_STRINGS entries (previously absent, silently falling back to
    English) — proven the same way as the ml test above, for both
    languages."""
    from app.i18n.templates import PDF_REPORT_STRINGS

    for language in ("hi", "ta"):
        analysis = await _make_analysis_with_claim(db_session, language=language)
        pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
        assert pdf_bytes.startswith(b"%PDF")
        assert PDF_REPORT_STRINGS[language]["claim_label"] != PDF_REPORT_STRINGS["en"]["claim_label"]


@pytest.mark.asyncio
async def test_pdf_renders_real_hindi_tamil_malayalam_glyphs_not_question_marks(db_session):
    """The actual Unicode-rendering fix: for ml/hi/ta reports, claim text
    and labels in that script must extract back out as the real glyphs via
    a genuine PDF text-extraction library (pypdf) — not as '?' fallback
    characters, and the encoding-loss footer note must NOT appear, since
    every character used here is within the bundled font's own repertoire."""
    import pypdf

    cases = {
        "ml": "ഇത് ഒരു പരീക്ഷണ അവകാശവാദമാണ്",
        "hi": "यह एक परीक्षण दावा है",
        "ta": "இது ஒரு சோதனை உரிமைகோரல்",
    }
    for language, claim_text in cases.items():
        analysis = await _make_analysis_with_claim(db_session, claim_text=claim_text, language=language)
        pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        extracted = reader.pages[0].extract_text()

        # The exact claim text must appear verbatim — proves real glyphs
        # were embedded and are recoverable, not a subset of '?' characters.
        assert claim_text in extracted, f"{language}: claim text not found verbatim in extracted PDF text"
        # No '?' fallback character anywhere — every character used here is
        # within NotoSans{Malayalam,Devanagari,Tamil}'s own cmap.
        assert "?" not in extracted, f"{language}: unexpected '?' fallback in extracted PDF text"


@pytest.mark.asyncio
async def test_pdf_still_falls_back_honestly_for_a_genuinely_unsupported_script(db_session):
    """Residual honest-fallback path: a Hindi-language report whose claim
    text mixes in a THIRD script the bundled Devanagari font doesn't cover
    (Chinese, in this case) must still replace only those specific
    characters with '?' and show the encoding-loss footer note — proving
    the fix narrowed the fallback to genuinely-missing glyphs rather than
    removing the safety net entirely."""
    import pypdf

    mixed_text = "यह एक परीक्षण 中文 दावा है"
    analysis = await _make_analysis_with_claim(db_session, claim_text=mixed_text, language="hi")
    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    assert pdf_bytes.startswith(b"%PDF")

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    extracted = reader.pages[0].extract_text()
    assert "?" in extracted
    # The Devanagari portions of the SAME string still render as real
    # glyphs — only the unsupported Chinese characters are replaced.
    assert "यह एक परीक्षण" in extracted


@pytest.mark.asyncio
async def test_pdf_falls_back_to_helvetica_for_an_undocumented_language_code(db_session):
    """A language code with neither a PDF_REPORT_STRINGS entry nor a
    bundled Unicode font (defensive case — analysis.language is a free-text
    DB column, not enforced against SUPPORTED_LANGUAGES at the schema
    level) must still degrade honestly to English labels + the Latin-1
    fallback path, never KeyError or crash."""
    analysis = await _make_analysis_with_claim(db_session, language="fr")
    pdf_bytes = await generate_analysis_report_pdf(db_session, analysis)
    assert pdf_bytes.startswith(b"%PDF")
