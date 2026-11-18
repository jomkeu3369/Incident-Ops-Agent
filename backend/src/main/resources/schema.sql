DROP TABLE IF EXISTS post_mortems;
DROP TABLE IF EXISTS remediation_executions;
DROP TABLE IF EXISTS remediation_approvals;
DROP TABLE IF EXISTS corpus_chunks;
DROP TABLE IF EXISTS incidents;

CREATE TABLE incidents (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    incident_code VARCHAR(32) NOT NULL UNIQUE,
    title VARCHAR(200) NOT NULL,
    service_name VARCHAR(100) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL,
    summary TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE corpus_chunks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    chunk_id VARCHAR(32) NOT NULL UNIQUE,
    corpus_type VARCHAR(16) NOT NULL,
    service_name VARCHAR(100) NOT NULL,
    title VARCHAR(200) NOT NULL,
    keywords VARCHAR(255),
    content MEDIUMTEXT NOT NULL,
    sequence_order INT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE remediation_approvals (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    approval_id VARCHAR(64) NOT NULL UNIQUE,
    incident_code VARCHAR(32) NOT NULL,
    action_command TEXT NOT NULL,
    proposed_by VARCHAR(100) NOT NULL,
    approved_by VARCHAR(100),
    decision VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE remediation_executions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    approval_id VARCHAR(64) NOT NULL UNIQUE,
    incident_code VARCHAR(32) NOT NULL,
    action_command TEXT NOT NULL,
    executed_by VARCHAR(100) NOT NULL,
    execution_status VARCHAR(16) NOT NULL,
    execution_output TEXT,
    executed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE post_mortems (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    incident_code VARCHAR(32) NOT NULL UNIQUE,
    approval_id VARCHAR(64) NOT NULL UNIQUE,
    root_cause TEXT NOT NULL,
    timeline_markdown TEXT NOT NULL,
    remediation_summary TEXT NOT NULL,
    prevention_items TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;