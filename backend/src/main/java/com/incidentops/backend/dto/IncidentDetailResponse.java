package com.incidentops.backend.dto;

import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.domain.PostMortem;
import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationExecution;
import java.util.List;

public record IncidentDetailResponse(
        Incident incident,
        List<RemediationApproval> approvals,
        List<RemediationExecution> executions,
        PostMortem postMortem
) {}