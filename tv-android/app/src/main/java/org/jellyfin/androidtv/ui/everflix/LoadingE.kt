package org.jellyfin.androidtv.ui.everflix

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import kotlinx.coroutines.delay
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.ui.home.HomeSelection

private var shownOnce = false

/**
 * Right after the intro, the E stays on screen breathing until the home screen has its first card,
 * so there is no empty "loading" moment. Only on launch, and never longer than 12 seconds.
 */
@Composable
fun LoadingE() {
	if (shownOnce) return
	val ready by HomeSelection.item.collectAsState()
	var timedOut by remember { mutableStateOf(false) }
	LaunchedEffect(Unit) {
		delay(12_000)
		timedOut = true
	}
	val visible = ready == null && !timedOut
	LaunchedEffect(visible) { if (!visible) shownOnce = true }

	AnimatedVisibility(visible = visible, exit = fadeOut(tween(400))) {
		val pulse = rememberInfiniteTransition(label = "loadingE")
		val glow by pulse.animateFloat(0.55f, 1f, infiniteRepeatable(tween(900), RepeatMode.Reverse), label = "glow")
		Box(Modifier.fillMaxSize().background(Color(0xFF0B0D12)), contentAlignment = Alignment.Center) {
			Image(
				painter = painterResource(R.drawable.everflix_e),
				contentDescription = null,
				modifier = Modifier.fillMaxHeight(700f / 1080f).aspectRatio(858f / 1124f).scale(0.97f + 0.03f * glow).alpha(glow),
			)
		}
	}
}
