package com.incidentops.backend.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationExecution;
import com.incidentops.backend.domain.RemediationPlan;
import com.incidentops.backend.domain.RemediationPlanStep;
import com.incidentops.backend.dto.ApprovalCreateRequest;
import com.incidentops.backend.dto.ApprovalDecisionRequest;
import com.incidentops.backend.dto.CompleteIncidentRequest;
import com.incidentops.backend.dto.PlanCreateRequest;
import com.incidentops.backend.exception.ConflictException;
import com.incidentops.backend.mapper.CorpusMapper;
import com.incidentops.backend.mapper.IncidentMapper;
import com.incidentops.backend.mapper.PostMortemMapper;
import com.incidentops.backend.mapper.RemediationMapper;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class IncidentServiceTest {
    private static final String INCIDENT = "INC-1";
    private static final String OTHER_INCIDENT = "INC-2";
    private static final String PLAN = "PLAN-1";
    private static final String APPROVAL = "APP-1";
    private static final String ACTION = "restart demo service";

    @Mock
    private IncidentMapper incidents;
    @Mock
    private RemediationMapper remediation;
    @Mock
    private PostMortemMapper postMortems;
    @Mock
    private CorpusMapper corpus;

    private IncidentService service;

    @BeforeEach
    void setUp() {
        service = new IncidentService(incidents, remediation, postMortems, corpus);
    }

    @Test
    void approvalCopiesTheActionFromTheStoredPlanStep() {
        lockIncident("OPEN");
        when(remediation.findPlanById(PLAN)).thenReturn(plan());

        service.createApproval(INCIDENT, approvalRequest());

        verify(remediation).insertApproval(argThat(approval ->
                ACTION.equals(approval.getActionCommand())
                        && PLAN.equals(approval.getPlanId())
                        && Integer.valueOf(2).equals(approval.getStepNo())));
        verify(incidents).updateStatus(INCIDENT, "WAITING_APPROVAL");
    }

    @Test
    void resolvedIncidentCannotBeReopenedByAnApproval() {
        lockIncident("RESOLVED");

        assertThatThrownBy(() -> service.createApproval(INCIDENT, approvalRequest()))
                .isInstanceOf(ConflictException.class);
        verify(remediation, never()).insertApproval(any());
    }

    @Test
    void unfinishedApprovalBlocksASecondApproval() {
        lockIncident("WAITING_APPROVAL");
        when(remediation.hasUnfinishedPlanApproval(INCIDENT)).thenReturn(true);

        assertThatThrownBy(() -> service.createApproval(INCIDENT, approvalRequest()))
                .isInstanceOf(ConflictException.class);
        verify(remediation, never()).insertApproval(any());
    }

    @Test
    void planRejectsEvidenceOutsideItsIncidentAndService() {
        lockIncident("OPEN");
        when(corpus.findScopedChunkIds(INCIDENT, "demo", Set.of("LOG-2"))).thenReturn(List.of());
        PlanCreateRequest request = new PlanCreateRequest(PLAN, "planner",
                List.of(new RemediationPlanStep(1, "점검", null, null, List.of("LOG-2"))), "지표 확인");

        assertThatThrownBy(() -> service.createPlan(INCIDENT, request))
                .isInstanceOf(IllegalArgumentException.class);
        verify(remediation, never()).insertPlan(any());
    }

    @Test
    void approvalDecisionConflictsIfPendingUpdateChangesNoRows() {
        lockIncident("WAITING_APPROVAL");
        when(remediation.findApprovalById(APPROVAL)).thenReturn(approval(INCIDENT, "PENDING"));
        when(remediation.updateApprovalDecision(APPROVAL, "operator", "REJECTED")).thenReturn(0);

        assertThatThrownBy(() -> service.decideApproval(
                INCIDENT, APPROVAL, new ApprovalDecisionRequest("operator", "REJECTED")))
                .isInstanceOf(ConflictException.class);
    }

    @Test
    void completionChecksOwnershipBeforeLoadingTheExistingExecution() {
        lockIncident("OPEN");
        when(remediation.findApprovalById(APPROVAL)).thenReturn(approval(OTHER_INCIDENT, "APPROVED"));

        assertThatThrownBy(() -> service.completeIncidentWithPostMortem(INCIDENT, completion("SUCCESS", "확인 완료")))
                .isInstanceOf(ConflictException.class);
        verify(remediation, never()).findExecutionByApprovalId(APPROVAL);
    }

    @Test
    void successfulCompletionRequiresVerificationBeforeWriting() {
        stubApprovedIncident("WAITING_APPROVAL");

        assertThatThrownBy(() -> service.completeIncidentWithPostMortem(INCIDENT, completion("SUCCESS", " ")))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("verificationSummary");
        verify(remediation, never()).insertExecution(any());
        verify(postMortems, never()).insert(any());
    }

    @Test
    void successfulCompletionPersistsTheVerificationSummary() {
        stubApprovedIncident("WAITING_APPROVAL");

        service.completeIncidentWithPostMortem(INCIDENT, completion("SUCCESS", "오류율 정상"));

        verify(remediation).insertExecution(argThat(execution -> "오류율 정상".equals(execution.getVerificationSummary())));
        verify(postMortems).insert(any());
        verify(incidents).updateStatus(INCIDENT, "RESOLVED");
    }

    @Test
    void identicalResultCanBeReplayedAfterResolution() {
        stubApprovedIncident("RESOLVED");
        RemediationExecution execution = recordedExecution("SUCCESS");
        when(remediation.findExecutionByApprovalId(APPROVAL)).thenReturn(execution);

        var response = service.completeIncidentWithPostMortem(INCIDENT, completion("SUCCESS", "오류율 정상"));

        assertThat(response.idempotentReplay()).isTrue();
        assertThat(response.execution()).isSameAs(execution);
        verify(remediation, never()).insertExecution(any());
    }

    @Test
    void failedExecutionCannotBeChangedToSuccessUsingTheSameApproval() {
        stubApprovedIncident("WAITING_APPROVAL");
        when(remediation.findExecutionByApprovalId(APPROVAL)).thenReturn(recordedExecution("FAILED"));

        assertThatThrownBy(() -> service.completeIncidentWithPostMortem(INCIDENT, completion("SUCCESS", "오류율 정상")))
                .isInstanceOf(ConflictException.class);
        verify(remediation, never()).insertExecution(any());
    }

    private void stubApprovedIncident(String status) {
        lockIncident(status);
        when(remediation.findApprovalById(APPROVAL)).thenReturn(approval(INCIDENT, "APPROVED"));
        when(remediation.findPlanById(PLAN)).thenReturn(plan());
    }

    private void lockIncident(String status) {
        when(incidents.lockByIncidentCode(INCIDENT)).thenReturn(
                Incident.builder().incidentCode(INCIDENT).serviceName("demo").status(status).build());
    }

    private ApprovalCreateRequest approvalRequest() {
        return new ApprovalCreateRequest(APPROVAL, PLAN, 2, "planner");
    }

    private RemediationPlan plan() {
        return RemediationPlan.builder()
                .planId(PLAN)
                .incidentCode(INCIDENT)
                .steps(List.of(new RemediationPlanStep(2, "복구", "운영자 승인", ACTION, List.of())))
                .build();
    }

    private RemediationApproval approval(String incidentCode, String decision) {
        return RemediationApproval.builder()
                .approvalId(APPROVAL)
                .incidentCode(incidentCode)
                .planId(PLAN)
                .decision(decision)
                .actionCommand(ACTION)
                .build();
    }

    private CompleteIncidentRequest completion(String status, String verification) {
        return new CompleteIncidentRequest(
                APPROVAL, ACTION, "operator", status, null, verification, "원인", "타임라인", null, "재발 방지");
    }

    private RemediationExecution recordedExecution(String status) {
        return RemediationExecution.builder()
                .approvalId(APPROVAL)
                .incidentCode(INCIDENT)
                .actionCommand(ACTION)
                .executionStatus(status)
                .build();
    }
}
