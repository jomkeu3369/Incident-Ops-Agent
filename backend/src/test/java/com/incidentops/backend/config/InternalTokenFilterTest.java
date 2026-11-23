package com.incidentops.backend.config;

import static org.assertj.core.api.Assertions.assertThat;

import jakarta.servlet.http.HttpServletResponse;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullAndEmptySource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

class InternalTokenFilterTest {

    private static final String APPROVAL_PATH = "/api/incidents/INC-001/approvals/APR-001";
    private static final String TEST_TOKEN = "configured-token";

    @ParameterizedTest
    @NullAndEmptySource
    @ValueSource(strings = {" "})
    void rejectsApprovalWhenTokenIsNotConfigured(String configuredToken) throws Exception {
        assertApprovalRejected(configuredToken, null);
        assertApprovalRejected(configuredToken, configuredToken);
    }

    @ParameterizedTest
    @NullAndEmptySource
    @ValueSource(strings = {"wrong-token", "short", "configured-token-extra"})
    void rejectsApprovalWithoutMatchingHeader(String providedToken) throws Exception {
        assertApprovalRejected(TEST_TOKEN, providedToken);
    }

    @Test
    void allowsApprovalWithMatchingHeader() throws Exception {
        MockHttpServletRequest request = approvalRequest(TEST_TOKEN);
        MockFilterChain chain = new MockFilterChain();

        new InternalTokenFilter(TEST_TOKEN).doFilter(request, new MockHttpServletResponse(), chain);

        assertThat(chain.getRequest()).isSameAs(request);
    }

    @ParameterizedTest
    @ValueSource(strings = {"/actuator/health", "/swagger-ui/index.html", "/v3/api-docs", "/v3/api-docs.yaml"})
    void allowsHealthAndSwaggerWithoutToken(String path) throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", path);
        request.setServletPath(path);
        MockFilterChain chain = new MockFilterChain();

        new InternalTokenFilter("").doFilter(request, new MockHttpServletResponse(), chain);

        assertThat(chain.getRequest()).isSameAs(request);
    }

    private void assertApprovalRejected(String configuredToken, String providedToken) throws Exception {
        MockHttpServletResponse response = new MockHttpServletResponse();
        MockFilterChain chain = new MockFilterChain();

        new InternalTokenFilter(configuredToken).doFilter(approvalRequest(providedToken), response, chain);

        assertThat(response.getStatus()).isEqualTo(HttpServletResponse.SC_UNAUTHORIZED);
        assertThat(response.getHeader("X-Content-Type-Options")).isEqualTo("nosniff");
        assertThat(response.getHeader("Cache-Control")).isEqualTo("no-store");
        assertThat(chain.getRequest()).isNull();
    }

    private MockHttpServletRequest approvalRequest(String providedToken) {
        MockHttpServletRequest request = new MockHttpServletRequest("PATCH", APPROVAL_PATH);
        request.setServletPath(APPROVAL_PATH);
        if (providedToken != null) {
            request.addHeader(InternalTokenFilter.HEADER_NAME, providedToken);
        }
        return request;
    }
}
