package org.jellyfin.androidtv.ui.everflix

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color

/** Everflix sign-in and "who's watching" background: a soft navy glow instead of Jellyfin's. */
@Composable
fun StartupBackground() {
	Box(
		Modifier
			.fillMaxSize()
			.background(Color(0xFF0B0D12))
			.background(Brush.radialGradient(listOf(Color(0xFF1A2550), Color(0x000B0D12)), radius = 1400f))
	)
}
