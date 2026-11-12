package org.jellyfin.androidtv.ui.playback.overlay.action

import android.content.Context
import android.view.View
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.ui.everflix.EfPicker
import org.jellyfin.androidtv.ui.playback.PlaybackController
import org.jellyfin.androidtv.ui.playback.VideoSpeedController
import org.jellyfin.androidtv.ui.playback.overlay.CustomPlaybackTransportControlGlue
import org.jellyfin.androidtv.ui.playback.overlay.VideoPlayerAdapter
import java.util.Locale

class PlaybackSpeedAction(
	context: Context,
	customPlaybackTransportControlGlue: CustomPlaybackTransportControlGlue,
	playbackController: PlaybackController
) : CustomAction(context, customPlaybackTransportControlGlue) {
	private val speedController = VideoSpeedController(playbackController)
	private val speeds = VideoSpeedController.SpeedSteps.entries.toTypedArray()
	private var popup: EfPicker? = null

	init {
		initializeWithIcon(R.drawable.ic_playback_speed)
	}

	override fun handleClickAction(
		playbackController: PlaybackController,
		videoPlayerAdapter: VideoPlayerAdapter,
		context: Context,
		view: View,
	) {
		videoPlayerAdapter.leanbackOverlayFragment.setFading(false)
		dismissPopup()
		popup = EfPicker(
			context, "Velocidad",
			speeds.map { step ->
				val label = if (step.speed == 1f) "Normal" else String.format(Locale("es"), "%s×", step.speed.toString().removeSuffix(".0").replace('.', ','))
				EfPicker.Option(label, step == speedController.currentSpeed)
			},
			onPick = { i -> speedController.currentSpeed = speeds[i] },
			onDismiss = {
				videoPlayerAdapter.leanbackOverlayFragment.setFading(true)
				popup = null
			},
		)
		popup?.show()
	}

	fun dismissPopup() {
		popup?.dismiss()
	}
}
