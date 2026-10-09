"""Public contracts for the recognition/validation pipeline."""

from .config import RecognitionConfig, load_recognition_config
from .aggregators import ConfigurableValidationAggregator
from .decision import DecisionEngine
from .evaluation import (
    evaluate_cross_validation,
    evaluate_term_extraction,
    evaluate_topic_identification,
)
from .documents import (
    adapt_legacy_document,
    load_documents_jsonl,
    write_term_candidates_jsonl,
    write_topic_candidates_jsonl,
)
from .errors import (
    PromptConfigurationError,
    RecognitionConfigurationError,
    RecognitionError,
    RecognitionNotFoundError,
    ValidatorResponseError,
    ValidatorUnavailableError,
)
from .interfaces import TermExtractor, TopicIdentifier, ValidationAggregator, ValidatorProvider
from .models import (
    AggregationStrategy,
    CandidateStatus,
    CrossValidationRequest,
    CrossValidationResult,
    Document,
    FinalDecision,
    ResourceDecision,
    TermCandidate,
    TermValidationRequest,
    TermValidationResult,
    TopicCandidate,
    TopicResolution,
    TopicValidationRequest,
    TopicValidationResult,
    ValidatorDecision,
    ValidatorResult,
)
from .prompts import PromptSnapshot, PromptTemplate, load_prompt
from .repository import RecognitionRepository
from .providers import (
    BuiltinRuleValidatorProvider,
    OpenAICompatibleChatTransport,
    OpenAICompatibleTopicLabelProvider,
    OpenAICompatibleValidatorProvider,
    ResolvedValidatorSettings,
    resolve_validator_settings,
)
from .term import (
    CompositeTermExtractor,
    KnowledgeSeedTermExtractor,
    LegacyLlmConsensusTermExtractor,
    LegacyStatisticalTermExtractor,
)
from .term_intersection import EvaluatedBaselineTermProvider, SentenceTermIntersectionProvider
from .runtime import RecognitionCoordinator, RecognitionRunOptions
from .units import build_recognition_units
from .validation import (
    CrossValidationService,
    PersistedTermValidation,
    TermValidationService,
    TopicValidationService,
)
from .topic import (
    AutoTopicIdentifier,
    ConstrainedTopicClassifier,
    ContentUnitTopicIdentifier,
    DiscoveredCluster,
    DiscoveryTopicIdentifier,
    LegacyBertopicDiscoveryBackend,
    SmallCorpusTopicDiscoveryBackend,
    SingleDocumentTopicDiscoveryBackend,
    TopicDiscoveryBackend,
    TopicLabel,
    TopicLabelProvider,
    TopicNode,
    normalize_topic_table,
    persist_topic_candidates,
)

__all__ = [
    "AggregationStrategy", "AutoTopicIdentifier", "CandidateStatus", "CompositeTermExtractor",
    "ConfigurableValidationAggregator", "CrossValidationRequest", "CrossValidationService",
    "CrossValidationResult", "Document", "FinalDecision", "PromptConfigurationError",
    "PromptSnapshot", "PromptTemplate", "RecognitionConfig",
    "RecognitionConfigurationError", "RecognitionError", "RecognitionNotFoundError",
    "RecognitionRepository", "RecognitionCoordinator", "RecognitionRunOptions", "ResolvedValidatorSettings",
    "KnowledgeSeedTermExtractor", "LegacyLlmConsensusTermExtractor",
    "LegacyStatisticalTermExtractor", "EvaluatedBaselineTermProvider", "SentenceTermIntersectionProvider",
    "ResourceDecision", "TermCandidate", "TermExtractor", "TermValidationRequest",
    "TermValidationResult", "TopicCandidate", "TopicDiscoveryBackend", "TopicIdentifier",
    "TopicLabel", "TopicLabelProvider", "TopicNode", "TopicResolution",
    "TopicValidationRequest", "TopicValidationResult", "ValidationAggregator",
    "TermValidationService", "TopicValidationService", "PersistedTermValidation", "DecisionEngine",
    "ValidatorDecision", "ValidatorProvider", "ValidatorResult",
    "ValidatorResponseError", "ValidatorUnavailableError",
    "BuiltinRuleValidatorProvider", "OpenAICompatibleChatTransport", "OpenAICompatibleTopicLabelProvider",
    "OpenAICompatibleValidatorProvider",
    "ConstrainedTopicClassifier", "ContentUnitTopicIdentifier", "DiscoveredCluster", "DiscoveryTopicIdentifier",
    "LegacyBertopicDiscoveryBackend", "SmallCorpusTopicDiscoveryBackend",
    "SingleDocumentTopicDiscoveryBackend",
    "adapt_legacy_document", "build_recognition_units", "load_documents_jsonl", "load_prompt",
    "load_recognition_config", "normalize_topic_table", "persist_topic_candidates",
    "resolve_validator_settings", "write_term_candidates_jsonl",
    "write_topic_candidates_jsonl", "evaluate_term_extraction",
    "evaluate_topic_identification", "evaluate_cross_validation",
]
