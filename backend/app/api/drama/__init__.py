"""Drama API package: /api/drama/*."""

from fastapi import APIRouter

from app.api.drama import agents, assets, canvas, collection, episodes, generation, import_script, projects, scripts, skills

router = APIRouter(prefix="/drama", tags=["drama"])
router.include_router(projects.router)
router.include_router(scripts.router)
router.include_router(import_script.router)
router.include_router(agents.router)
router.include_router(assets.router)
router.include_router(episodes.router)
router.include_router(generation.router)
router.include_router(canvas.router)
router.include_router(skills.router)
router.include_router(collection.router)
