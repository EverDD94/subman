package org.jellyfin.androidtv.ui.startup.fragment

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.fragment.app.Fragment
import androidx.fragment.compose.content
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.ui.base.JellyfinTheme

@Composable
fun SplashScreen() {
	// Flat app background and the E at the same size and place as the last frame of the intro
	Box(
		modifier = Modifier
			.fillMaxSize()
			.background(Color(0xFF0B0D12)),
		contentAlignment = Alignment.Center,
	) {
		Image(
			painter = painterResource(R.drawable.everflix_e),
			contentDescription = stringResource(R.string.app_name),
			modifier = Modifier
				.fillMaxHeight(INTRO_E_HEIGHT)
				.aspectRatio(858f / 1124f),
		)
	}
}

/** Height of the settled E in the intro video, as a fraction of the screen height (700 / 1080). */
private const val INTRO_E_HEIGHT = 700f / 1080f

class SplashFragment : Fragment() {
	override fun onCreateView(
		inflater: LayoutInflater,
		container: ViewGroup?,
		savedInstanceState: Bundle?
	) = content {
		JellyfinTheme {
			SplashScreen()
		}
	}
}
