from slackclient import SlackClient
from datetime    import timedelta
from subprocess  import *
from websocket   import *
import time
import traceback
import sys
import scrapeKnl
import scrapeEtre
import threading

commandKey        = 'bash'
commandChannel    = 'C4UC35TLN'
dbId              = 'U4SDBCXBJ'
debugSlackChannel = 'robot_comms'
scrapeKey         = 'scrape'
scrapeOptionsMap  = {'knl' : scrapeKnl.main, 'etre' : scrapeEtre.main}
helpKey           = 'help'
maxKeepAlive      = 30
#todo help key / list commands

#todo need logging

#todo global thread dictionary to keep track of manual scrapes

def handleText( text, channel, user ):

	print 'handleText: ' + text + ' ' + channel + ' ' + user

	if not text:
		print 'ignoring empty message'
		return ''

	if 'who knows' in text:
		return 'Jeff knows.'

	if 'llama' in text:
		return 'Tinaface!'

	if 'regulators' in text.lower():
		return 'Mount up!'

	if 'destiny' in text.lower():
		return 'Eyes up, guardian.'

	if 'what a save' in text.lower():
		return 'SAVAGE'

	if 'name' in text.lower():
		return 'droopy weiner, lol'

	if 'stout' in text.lower():
		return 'dimah dozen'

	if 'rockin' in text.lower():
		return "Rockin, rockin and rollin\ndown to the beach I'm strollin\nbut the seagulls poke at my head\nNOT FUN\n but the seagulls\nhmm\nstop it now\nHOOOHAAAHOOHOOHOHOHA\nHOOHAHOHOHOHA\nHOOOHAHOHOHOHOHAHOHAHOHOHA\n"

	if 'ok' in text.lower():
		return 'WHAT A SAVE'

	if 'tired' in text.lower():
		return 'go to sleep, h0'

	if 'yeah' in text.lower():
		return 'SOLAR ECLIPSES'

	#split on spaces and grab the first word
	key = text.split(" ")
	if  len( key ) == 0:
		print 'empty key!'
		return ''

	key = key[0]
	print key
	#handle scraping
	if key.lower() == scrapeKey.lower():
		
		return handleScrape( text.replace( scrapeKey + " ", "" ) )

	#handle commands
	if key.lower() == commandKey.lower():
		#there can only be one
		if user != dbId:
			return 'Sorry, you do not have permission do execute that command.'

		return handleCommand( text.replace( commandKey + " ", "" ) )

	return ''

def handleCommand( command ):
	print 'handleCommand: ' + str( command )

	try:

		p              = Popen( command, shell=True, stdout=PIPE )
		stdout, stderr = p.communicate()
		
		print stdout
		print stderr

		return str( stdout ) + '\n' + str( stderr )

	except Exception as e:
		exc_type, exc_value, exc_tb = sys.exc_info()
		return False, 'Caught ' + str( traceback.format_exception( exc_type, exc_value, exc_tb ) ) 

def handleScrape( command ):

	print 'handleScrape: ' + command

	if command.lower() in scrapeOptionsMap:

		target   = scrapeOptionsMap[ command.lower() ]
		t        = threading.Thread( target=target, args=() )
		t.daemon = True
		t.start()

		return 'Acknowledged command, started manual ' + command + ' scraping'

	#invalid scrape option
	return "Invalid scrape option: " + command + '\nAvailable options: ' + ', '.join( scrapeOptionsMap.keys() )

def handleHelp():
	helpMessage = ""
	return helpMessage

def sendReply( sc, ts, channelId, replyText ):

	print ts + ' ' + channelId + ' ' + replyText

	output = sc.api_call(
	  'chat.postMessage',
	  ts         = ts,
	  channel    = channelId,
	  text       = replyText
	)
	print output

def main():

	sleepTimeSeconds          = 60
	allowableTimeDeltaSeconds = 3 * sleepTimeSeconds
	slackToken                = "";

	with open( './slackToken' ) as f:  
		slackToken = str( f.read() ).strip()

	sc = SlackClient( slackToken.strip() )

	if sc.rtm_connect():

		keepAliveCount = 0

		while True:

			try:

				r = sc.rtm_read()
				print str( time.time() ) + ' ' + str( r )

				#check time, if it's the time delta is greater than our polling then       
				if len( r ) > 0:
					attemptCount = 0
					response     = r[0]
					#check for relevant time key

					if 'ts' in response:
						
						ts                 = response['ts'] 
						elapsedTimeSeconds = time.time() - float( ts )
						
						if elapsedTimeSeconds < allowableTimeDeltaSeconds:

							if 'channel' in response and 'user' in response:
								#get the message
								text = ''
								if 'text' in response:
									text = response['text']

								replyText = handleText( text, response['channel'], response['user'] )

								if replyText:
									sendReply( sc, ts, response['channel'], replyText )
								else:
									print 'replyText is empty!'

							if 'channel' not in response:
								print 'No Channel in response!'

							if 'user' not in response:
								print 'No user in response!'

						else:
							print 'ignoring stale response, elapsedTimeSeconds=' + str( timedelta( seconds=( elapsedTimeSeconds ) ) )

				if not r and keepAliveCount > maxKeepAlive:
					print 'Sleeping ' + str( sleepTimeSeconds ) + 's'
					time.sleep( sleepTimeSeconds )
					keepAliveCount = 0
				else:
					time.sleep( 1 )
					keepAliveCount += 1

			except WebSocketConnectionClosedException:
				exc_type, exc_value, exc_tb = sys.exc_info()
				print 'Caught ' + str( traceback.format_exception( exc_type, exc_value, exc_tb ) )
				#try to re-connect
				sc.rtm_connect()

			except Exception as e:
				exc_type, exc_value, exc_tb = sys.exc_info()
				print 'Caught ' + str( traceback.format_exception( exc_type, exc_value, exc_tb ) ) 

		else:
			print "Connection Failed, invalid token?"

if __name__ == "__main__":
	main()