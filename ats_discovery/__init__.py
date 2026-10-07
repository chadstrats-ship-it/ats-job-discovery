"""ats-job-discovery: fetch, filter and score postings from public ATS job-board APIs."""
from .fetchers import FETCHERS, fetch_jobs
from .filters import FilterConfig, evaluate_filters
from .models import ATS_NAMES, Job
from .scoring import ScoredJob, ScoringConfig, evaluate, load_config, rank, score_job
from .transport import FetchError, FixtureHttp, NotFound, UrllibHttp

__version__ = "0.1.0"

__all__ = [
    "ATS_NAMES", "FETCHERS", "FetchError", "FilterConfig", "FixtureHttp", "Job", "NotFound", "ScoredJob",
    "ScoringConfig", "UrllibHttp", "evaluate", "evaluate_filters", "fetch_jobs", "load_config", "rank", "score_job",
]
