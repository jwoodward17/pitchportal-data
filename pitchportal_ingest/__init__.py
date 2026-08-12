"""PitchPortal Statcast ingestion.

Pulls Statcast via pybaseball and caches the app's reference data (league
movement/velocity averages + featured pro arsenals) as JSON the app reads.

See README.md. Entry points:
  python -m pitchportal_ingest.pipeline      # build the cache
  uvicorn pitchportal_ingest.server:app      # serve the cache to the app
"""

__version__ = "0.1.0"
