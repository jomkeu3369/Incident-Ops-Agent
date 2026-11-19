ALTER TABLE corpus_chunks
    ADD COLUMN incident_code VARCHAR(32) NULL,
    ADD CONSTRAINT fk_corpus_chunks_incident
        FOREIGN KEY (incident_code) REFERENCES incidents (incident_code);
