"""Voice and text-to-speech services used by the desktop bridge."""

from .tts_text import TtsSentenceBuffer, split_tts_sentences
from .voice_assets import VoiceAssetLifecycle
from .voice_handler import DesktopVoiceHandler
from .voice_models import VoiceOperationMetrics, VoiceServiceError, VoiceSynthesisResult
from .voice_service import VoiceService

__all__ = [
    "DesktopVoiceHandler",
    "TtsSentenceBuffer",
    "VoiceAssetLifecycle",
    "VoiceOperationMetrics",
    "VoiceService",
    "VoiceServiceError",
    "VoiceSynthesisResult",
    "split_tts_sentences",
]
