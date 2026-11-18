package com.incidentops.backend.dto;

import com.incidentops.backend.domain.PostMortem;
import com.incidentops.backend.domain.RemediationExecution;

public record CompleteIncidentResponse(
        String incidentCode,
        String incidentStatus,
        boolean idempotentReplay,
        RemediationExecution execution,
        PostMortem postMortem
) {}