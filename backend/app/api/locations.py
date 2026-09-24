from fastapi import APIRouter, HTTPException

from app.db.storage import get_store
from app.models.schemas import Location, LocationCreate, LocationUpdate
from app.services.indexer import schedule_reindex

router = APIRouter(
    prefix="/projects/{project_id}/locations",
    tags=["locations"],
)


@router.get("", response_model=list[Location])
def list_locations(project_id: str):
    try:
        return get_store().list_locations(project_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("", response_model=Location, status_code=201)
def create_location(project_id: str, payload: LocationCreate):
    try:
        location = get_store().add_location(project_id, payload)
        schedule_reindex(project_id)
        return location
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/{location_id}", response_model=Location)
def get_location(project_id: str, location_id: str):
    try:
        return get_store().get_location(project_id, location_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.patch("/{location_id}", response_model=Location)
def update_location(project_id: str, location_id: str, payload: LocationUpdate):
    try:
        location = get_store().update_location(project_id, location_id, payload)
        schedule_reindex(project_id)
        return location
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/{location_id}", status_code=204)
def delete_location(project_id: str, location_id: str):
    try:
        get_store().delete_location(project_id, location_id)
        schedule_reindex(project_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e