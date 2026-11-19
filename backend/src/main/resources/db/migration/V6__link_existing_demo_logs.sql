-- Only associate legacy demo logs attributed to the affected service.
UPDATE corpus_chunks AS chunk
JOIN incidents AS incident
    ON incident.incident_code = 'INC-2026-001'
    AND incident.service_name = chunk.service_name
SET chunk.incident_code = incident.incident_code
WHERE chunk.incident_code IS NULL
    AND chunk.corpus_type = 'LOG'
    AND chunk.service_name = 'payment-service'
    AND chunk.chunk_id IN ('LOG-101', 'LOG-102', 'LOG-103', 'LOG-104', 'LOG-105');
