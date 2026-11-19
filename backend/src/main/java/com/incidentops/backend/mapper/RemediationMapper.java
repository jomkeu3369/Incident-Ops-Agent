package com.incidentops.backend.mapper;

import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationExecution;
import com.incidentops.backend.domain.RemediationPlan;
import java.util.List;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface RemediationMapper {
    RemediationPlan findPlanById(@Param("planId") String planId);
    List<RemediationPlan> findPlansByIncidentCode(@Param("incidentCode") String incidentCode);
    int insertPlan(RemediationPlan plan);

    RemediationApproval findApprovalById(@Param("approvalId") String approvalId);
    List<RemediationApproval> findApprovalsByIncidentCode(@Param("incidentCode") String incidentCode);
    boolean hasUnfinishedPlanApproval(@Param("incidentCode") String incidentCode);
    int insertApproval(RemediationApproval approval);
    int updateApprovalDecision(
            @Param("approvalId") String approvalId,
            @Param("approvedBy") String approvedBy,
            @Param("decision") String decision
    );

    RemediationExecution findExecutionByApprovalId(@Param("approvalId") String approvalId);
    List<RemediationExecution> findExecutionsByIncidentCode(@Param("incidentCode") String incidentCode);
    int insertExecution(RemediationExecution execution);
}
