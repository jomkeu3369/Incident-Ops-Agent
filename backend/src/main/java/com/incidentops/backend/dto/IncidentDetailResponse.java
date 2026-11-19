package com.incidentops.backend.dto;

import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.domain.PostMortem;
import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationExecution;
import com.incidentops.backend.domain.RemediationPlan;
import java.util.List;

public record IncidentDetailResponse(
        Incident incident,
        List<RemediationPlan> plans,
        List<RemediationApproval> approvals,
        List<RemediationExecution> executions,
        PostMortem postMortem
) {}
