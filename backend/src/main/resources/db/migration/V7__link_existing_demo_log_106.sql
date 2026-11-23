-- Link LOG-106 left unlinked by V6 for demo incident INC-2026-001.
UPDATE corpus_chunks AS chunk
JOIN incidents AS incident
    ON incident.incident_code = 'INC-2026-001'
    AND incident.service_name = chunk.service_name
SET chunk.incident_code = incident.incident_code
WHERE chunk.incident_code IS NULL
    AND chunk.corpus_type = 'LOG'
    AND chunk.service_name = 'payment-service'
    AND chunk.chunk_id = 'LOG-106';
