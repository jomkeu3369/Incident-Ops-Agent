package com.incidentops.backend.mapper;

import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationExecution;
import java.util.List;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface RemediationMapper {
    RemediationApproval findApprovalById(@Param("approvalId") String approvalId);
    List<RemediationApproval> findApprovalsByIncidentCode(@Param("incidentCode") String incidentCode);
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