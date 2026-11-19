CREATE TABLE remediation_plans (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    plan_id VARCHAR(64) NOT NULL UNIQUE,
    incident_code VARCHAR(32) NOT NULL,
    steps JSON NOT NULL,
    verification_criteria TEXT NOT NULL,
    created_by VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_remediation_plans_incident
        FOREIGN KEY (incident_code) REFERENCES incidents (incident_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
